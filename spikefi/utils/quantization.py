from torch import Tensor


class Quantizer:
    def __init__(self, precision: int, xmin: float, xmax: float, qmin: int = 0) -> None:
        self.precision = precision

        self.xmin = xmin
        self.xmax = xmax

        self.qmin = qmin
        self.qmax = 2. ** self.precision - 1 - self.qmin

        self.scale = (self.xmax - self.xmin) / (self.qmax - self.qmin)

        self.zero_point = self.qmin - self.xmin / self.scale
        if self.zero_point < self.qmin:
            self.zero_point = self.qmin
        elif self.zero_point > self.qmax:
            self.zero_point = self.qmax
        self.zero_point = int(self.zero_point)

    def quantize(self, x: Tensor) -> Tensor:
        assert x >= self.xmin and x <= self.xmax

        q = self.zero_point + x / self.scale

        q.clamp_(self.qmin, self.qmax).round_()
        q = q.round().byte()

        return q

    def dequantize(self, q: Tensor) -> Tensor:
        assert q >= self.qmin and q <= self.qmax

        return self.scale * (q - self.zero_point)

    def __repr__(self) -> str:
        s = "Quantizer:\n"
        s += f"  - Precision: {self.precision} bits\n"
        s += f"  - Real values range: {self.xmin} to {self.xmax}\n"
        s += f"  - Quantized values range: {self.qmin} to {self.qmax}"

        return s
