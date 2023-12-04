import argparse
import logging
import time
from typing import Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
from networks import get_network
from transformers import (
    FlattenTransformer,
    LeakyReLUTransformer,
    LinearTransformer,
    Polygon,
)
from utils.loading import parse_spec

DEVICE = "cpu"

torch.set_printoptions(threshold=10_000)

def output_size(conv, wh):
    w, h = wh
    output = [
        (wh[0] + 2 * conv.padding[0] - conv.kernel_size[0]) // conv.stride[0] + 1,
        (wh[1] + 2 * conv.padding[1] - conv.kernel_size[1]) // conv.stride[1] + 1,
    ]
    return output


def encode_loc(tup, shape):
    residual = 0
    coefficient = 1
    for i in list(range(len(shape)))[::-1]:
        residual = residual + coefficient * tup[i]
        coefficient = coefficient * shape[i]
    return residual


def conv_linear(conv, wh):
    with torch.no_grad():
        w, h = wh
        output = output_size(conv, wh)

        in_shape = (conv.in_channels, w, h)
        out_shape = (conv.out_channels, output[0], output[1])

        fc = nn.Linear(in_features=np.prod(in_shape), out_features=np.prod(out_shape))
        fc.weight.data.fill_(0.0)

        # Output coordinates
        for x_0 in range(output[0]):
            for y_0 in range(output[1]):
                x_00 = conv.stride[0] * x_0 - conv.padding[0]
                y_00 = conv.stride[1] * y_0 - conv.padding[1]
                for xd in range(conv.kernel_size[0]):
                    for yd in range(conv.kernel_size[1]):
                        for c1 in range(conv.out_channels):
                            fc.bias[encode_loc((c1, x_0, y_0), out_shape)] = conv.bias[
                                c1
                            ]
                            for c2 in range(conv.in_channels):
                                if 0 <= x_00 + xd < w and 0 <= y_00 + yd < h:
                                    cw = conv.weight[c1, c2, xd, yd]
                                    fc.weight[
                                        encode_loc((c1, x_0, y_0), out_shape),
                                        encode_loc(
                                            (c2, x_00 + xd, y_00 + yd), in_shape
                                        ),
                                    ] = cw
        return fc


def analyze(
        net: torch.nn.Sequential, inputs: torch.Tensor, eps: float, true_label: int,
        early_stopping: bool = False,
) -> bool:
    start_time = time.time()

    # add the 'batch' dimension
    inputs = inputs.unsqueeze(0)
    flattened = False
    num_conv = 0

    input_size = [(inputs.shape[-2], inputs.shape[-1])]
    for layer in net.children():
        if not isinstance(layer, torch.nn.Conv2d):
            break
        else:
            input_size.append(tuple(output_size(layer, input_size[num_conv])))
            num_conv += 1

    n_classes = list(net.children())[-1].out_features
    final_layer = torch.nn.Linear(in_features=n_classes, out_features=n_classes - 1)

    final_layer_weights = -torch.eye(n_classes)
    final_layer_weights = torch.cat(
        (final_layer_weights[:true_label], final_layer_weights[true_label + 1 :])
    )
    final_layer_weights[:, true_label] = 1.0

    final_layer.weight.data = final_layer_weights
    final_layer.bias.data[:] = 0.0

    net_layers = list(net.children()) + [final_layer]

    # Construct a model like net that passes Polygon through each layer
    transformer_layers = []
    location = 0
    in_polygon: Polygon = Polygon.create_from_input(inputs, eps=eps)
    x = in_polygon

    def add_layer(new_layer):
        nonlocal transformer_layers, x
        transformer_layers.append(new_layer)
        x = new_layer(x)

    for layer in net_layers:
        if isinstance(layer, torch.nn.Flatten) and not flattened:
            add_layer(FlattenTransformer())
        elif isinstance(layer, torch.nn.Linear):
            add_layer(LinearTransformer(layer.weight.data, layer.bias.data))
        elif isinstance(layer, torch.nn.ReLU):
            add_layer(LeakyReLUTransformer(negative_slope=0.0, init_polygon=x))
        elif isinstance(layer, torch.nn.LeakyReLU):
            add_layer(LeakyReLUTransformer(
                negative_slope=layer.negative_slope, init_polygon=x
            ))
        elif isinstance(layer, torch.nn.Conv2d):
            fc = conv_linear(layer, input_size[location])
            location += 1
            if not flattened:
                add_layer(FlattenTransformer())
                flattened = True
            add_layer(LinearTransformer(fc.weight, fc.bias))
        else:
            raise Exception(f"Unknown layer type {layer.__class__.__name__}")
    polygon_model = nn.Sequential(*transformer_layers)

    verified, epochs_trained = train(
        polygon_model=polygon_model, in_polygon=in_polygon, max_epochs=100, early_stopping=early_stopping
    )

    logging.info(f"The computation took {time.time() - start_time:.1f} seconds, {epochs_trained} epochs")
    return verified


def train(
        polygon_model: torch.nn.Sequential,
        in_polygon: Polygon,
        max_epochs: int | None = None,
        early_stopping: bool = False,
) -> Tuple[bool, int]:
    trainable = len(list(polygon_model.parameters())) > 0
    optimizer = None
    if trainable:
        optimizer = torch.optim.SGD(polygon_model.parameters(), lr=1.0)

    epoch = 1
    previous_loss: Optional[torch.Tensor] = None
    while max_epochs is None or epoch <= max_epochs:
        out_polygon: Polygon = polygon_model(in_polygon)
        lower_bounds, _ = out_polygon.evaluate()

        verified: bool = torch.all(lower_bounds > 0).item()  # type: ignore
        if verified:
            return True, epoch
        if not optimizer:
            return False, epoch

        loss = lower_bounds.clamp(max=0).abs().sum()
        if early_stopping:
            if previous_loss is not None and loss >= previous_loss:
                return False, epoch
            previous_loss = loss

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # Clamp all alpha values after each step
        for layer in polygon_model.children():
            if isinstance(layer, LeakyReLUTransformer):
                layer.clamp()

        epoch += 1

    return False, epoch


def get_gt(net, spec):
    with open("test_cases/gt.txt", "r") as f:
        for line in f.read().splitlines():
            model, fl, answer = line.split(",")
            if model == net and fl in spec:
                return answer


def main():
    parser = argparse.ArgumentParser(
        description="Neural network verification using DeepPoly relaxation."
    )
    parser.add_argument(
        "--net",
        type=str,
        choices=[
            "fc_base",
            "fc_1",
            "fc_2",
            "fc_3",
            "fc_4",
            "fc_5",
            "fc_6",
            "fc_7",
            "conv_base",
            "conv_1",
            "conv_2",
            "conv_3",
            "conv_4",
            "fc_lecture",
        ],
        required=True,
        help="Neural network architecture which is supposed to be verified.",
    )
    parser.add_argument("--spec", type=str, required=True, help="Test case to verify.")
    parser.add_argument(
        "--check",
        help="Whether to check the GT answer.",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "--early-stopping",
        help="Whether to early-stop training when loss increases.",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "--log",
        type=str,
    )
    args = parser.parse_args()
    if args.log:
        logging.basicConfig(level=args.log.upper())

    true_label, dataset, image, eps = parse_spec(args.spec)

    # print(args.spec)

    net = get_network(args.net, dataset, f"models/{dataset}_{args.net}.pt").to(DEVICE)

    image = image.to(DEVICE)
    out = net(image.unsqueeze(0))

    pred_label = out.max(dim=1)[1].item()
    assert pred_label == true_label

    verified = analyze(net, image, eps, true_label, early_stopping=args.early_stopping)
    verified_text = "verified" if verified else "not verified"
    print(verified_text)

    if args.check:
        gt = get_gt(args.net, args.spec)
        if verified_text == gt:
            print("^ correct\n")
        else:
            print(f"! incorrect, expected {gt}\n")


if __name__ == "__main__":
    main()
