import logging
from dataclasses import dataclass
from typing import Optional

import torch
from torch import Tensor


@dataclass
class Polygon:
    """
    Polygon weights have a shape (batch, out, in),
    while biases have a shape (batch, out), where
    - `out` is the dimension of the output of the layer outputting this DeepPoly, and
    - `in` is the dimension of the input of this layer
    We hereby refer to the shape of weights as the shape of the Polygon itself.
    """

    l_coefs: Tensor  # (batch, out, in)
    u_coefs: Tensor  # (batch, out, in)
    l_bias: Tensor  # (batch, out)
    u_bias: Tensor  # (batch, out)

    l_bound: Tensor  # (batch, out)
    u_bound: Tensor  # (batch, out)

    parent: Optional["Polygon"]

    def __init__(
        self,
        l_coefs: Tensor,
        u_coefs: Tensor,
        l_bias: Tensor,
        u_bias: Tensor,
        parent: Optional["Polygon"],
    ):
        self.l_coefs = l_coefs
        self.u_coefs = u_coefs
        self.l_bias = l_bias
        self.u_bias = u_bias
        self.parent = parent

        # Repeated back-substitution to calculate the bounds

        while parent is not None:
            # TODO consider early stopping if there is no dependency on the parent

            l_coefs_new = (
                torch.clamp(l_coefs, min=0) @ parent.l_coefs
                + torch.clamp(l_coefs, max=0) @ parent.u_coefs
            )
            u_coefs_new = (
                torch.clamp(u_coefs, min=0) @ parent.u_coefs
                + torch.clamp(u_coefs, max=0) @ parent.l_coefs
            )

            l_bias_new = l_bias + (
                (torch.clamp(l_coefs, min=0) * parent.l_bias.unsqueeze(1)).sum(-1)
                + (torch.clamp(l_coefs, max=0) * parent.u_bias.unsqueeze(1)).sum(-1)
            )
            u_bias_new = u_bias + (
                (torch.clamp(u_coefs, min=0) * parent.u_bias.unsqueeze(1)).sum(-1)
                + (torch.clamp(u_coefs, max=0) * parent.l_bias.unsqueeze(1)).sum(-1)
            )

            l_coefs, u_coefs, l_bias, u_bias = (
                l_coefs_new,
                u_coefs_new,
                l_bias_new,
                u_bias_new,
            )
            parent = parent.parent

        self.l_bound = l_bias
        self.u_bound = u_bias

    def __str__(self) -> str:
        lb, ub = self.evaluate()

        result = "Polygon(\n"
        batch, out, in_ = tuple(self.l_coefs.shape)
        result += f"  shape: ({batch=}, {out=}, in={in_})\n\n"
        for b in range(batch):
            for j in range(out):
                result += f"  o{j} ∈ [{lb[b, j]}, {ub[b, j]}]\n"
                l_coefs = " + ".join(
                    f"({c} × i{i})" for i, c in enumerate(self.l_coefs[b, j])
                )
                result += f"  o{j} ≥ {l_coefs} + {self.l_bias[b, j]}\n"
                u_coefs = " + ".join(
                    f"({c} × i{i})" for i, c in enumerate(self.u_coefs[b, j])
                )
                result += f"  o{j} ≤ {u_coefs} + {self.u_bias[b, j]}\n"
                result += "\n"
        result = result.strip() + "\n)"
        return result

    @staticmethod
    def create_from_input(input_tensor: torch.Tensor, eps: float) -> "Polygon":
        """
        - input shape: (batch, source)
        - output: Polygon with shape (batch, source, 0) and bounds fixed to input_tensor +- eps
        """
        batch, *dims = input_tensor.shape
        input_size = torch.prod(torch.tensor(dims)).item()

        polygon = Polygon(
            l_coefs=torch.zeros((batch, input_size, 0)),
            u_coefs=torch.zeros((batch, input_size, 0)),
            l_bias=torch.clamp(
                input_tensor.reshape((batch, input_size)) - eps, min=0, max=1
            ),
            u_bias=torch.clamp(
                input_tensor.reshape((batch, input_size)) + eps, min=0, max=1
            ),
            # Setting parent=None will cause a trivial zero-step back substitution, which essentially sets
            # l_bound, u_bound := l_bias, u_bias
            parent=None,
        )
        logging.debug(f"Created\n{polygon}")

        return polygon

    def evaluate(self) -> tuple[Tensor, Tensor]:
        """
        - output: a tuple of lower and upper bounds, each of shape (batch, *out)
        """
        return self.l_bound, self.u_bound


class FlattenTransformer(torch.nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x: Polygon) -> Polygon:
        return x


class LinearTransformer(torch.nn.Module):
    weight: Tensor  # (out, in)
    bias: Tensor  # (out)

    def __init__(self, weight: Tensor, bias: Tensor):
        super().__init__()
        self.weight = weight
        self.bias = bias

    def forward(self, x: Polygon) -> Polygon:
        """
        - x shape: (batch, out, in)
        - output shape: (batch, out_new, out)
        """
        polygon = Polygon(
            l_coefs=self.weight.unsqueeze(0),
            u_coefs=self.weight.unsqueeze(0),
            l_bias=self.bias.unsqueeze(0),
            u_bias=self.bias.unsqueeze(0),
            parent=x,
        )

        logging.debug(f"Linear layer output:\n{polygon}")
        return polygon


class ReLUTransformer(torch.nn.Module):
    def __init__(self):
        super().__init__()

    def relu_I(self, l_bound, u_bound, l_coefs, l_bias, u_coefs, u_bias, is_crossing):
        _, n = l_coefs.shape[:2]
        # For lower bound we clip the inequality to 0
        l_coefs[is_crossing] = 0
        l_bias[is_crossing] = 0
        # For upper bound, we calculate the slope 𝜆 and then apply  y ≤ 𝜆 * (x − l_x)
        slope = u_bound[is_crossing] / (u_bound[is_crossing] - l_bound[is_crossing])
        u_coefs[is_crossing] = torch.eye(n=n).unsqueeze(0)[
            is_crossing
        ] * slope.unsqueeze(-1)
        u_bias[is_crossing] = -slope * l_bound[is_crossing]

        return l_coefs, l_bias, u_coefs, u_bias

    def relu_II(self, l_bound, u_bound, l_coefs, l_bias, u_coefs, u_bias, is_crossing):
        _, n = l_coefs.shape[:2]
        # For lower bound, we apply y ≥ x
        l_coefs[is_crossing] = torch.eye(n=n).unsqueeze(0)[is_crossing]
        l_bias[is_crossing] = 0
        # For upper bound, we calculate the slope 𝜆 and then apply  y ≤ 𝜆 * (x − l_x)
        slope = u_bound[is_crossing] / (u_bound[is_crossing] - l_bound[is_crossing])
        u_coefs[is_crossing] = torch.eye(n=n).unsqueeze(0)[
            is_crossing
        ] * slope.unsqueeze(-1)
        u_bias[is_crossing] = -slope * l_bound[is_crossing]

        return l_coefs, l_bias, u_coefs, u_bias

    def forward(self, x: Polygon) -> Polygon:
        """
        - x shape: (batch, out, in)
        - output shape: (batch, out, out)
        """
        l_bound, u_bound = x.evaluate()
        batch, n = x.l_coefs.shape[:2]

        l_coefs = torch.zeros((batch, n, n))
        u_coefs = torch.zeros((batch, n, n))
        l_bias = torch.zeros((batch, n))
        u_bias = torch.zeros((batch, n))

        is_always_negative: torch.Tensor = u_bound <= 0
        # In this case the output of this neuron is always 0 regardless of the network input,
        # so we clip both the lower and upper constraint inequalities to 0
        l_coefs[is_always_negative] = 0
        u_coefs[is_always_negative] = 0
        l_bias[is_always_negative] = 0
        u_bias[is_always_negative] = 0

        is_always_positive: torch.Tensor = l_bound >= 0
        # In this case the output of this neuron is always equal to the previous neuron's output
        l_coefs[is_always_positive] = torch.eye(n=n).unsqueeze(0)[is_always_positive]
        u_coefs[is_always_positive] = torch.eye(n=n).unsqueeze(0)[is_always_positive]
        l_bias[is_always_positive] = 0
        u_bias[is_always_positive] = 0

        is_crossing = ~(is_always_negative | is_always_positive)

        poly_I = self.relu_I(
            l_bound, u_bound, l_coefs, l_bias, u_coefs, u_bias, is_crossing
        )

        poly_II = self.relu_II(
            l_bound, u_bound, l_coefs, l_bias, u_coefs, u_bias, is_crossing
        )

        # Pick the ReLU relaxation based on the minimal area heuristic
        # See Exercise 4.1 a)
        final_poly = []
        for a, b in zip(poly_I, poly_II):
            final_poly.append(torch.where(u_bound <= -l_bound, a, b))
        l_coefs, l_bias, u_coefs, u_bias = final_poly

        polygon = Polygon(
            l_coefs=l_coefs,
            u_coefs=u_coefs,
            l_bias=l_bias,
            u_bias=u_bias,
            parent=x,
        )
        logging.debug(f"ReLU output:\n{polygon}")
        return polygon
