r"""Cubic B-spline free-form deformations."""

from __future__ import annotations

from copy import copy as shallow_copy
import math
from typing import Callable, Optional, Sequence, TypeVar, Union, cast

import torch
from torch import Tensor
from torch.nn import init

from ...core import functional as U
from ...core.enum import PaddingMode
from ...core.grid import Grid
from ...core import kernels as K
from ...modules import ExpFlow

from .base import NonRigidTransform
from .parametric import ParametricTransform


TBSplineTransform = TypeVar("TBSplineTransform", bound="BSplineTransform")


class BSplineTransform(ParametricTransform, NonRigidTransform):
    r"""Non-rigid transformation parameterized by cubic B-spline function."""

    def __init__(
        self,
        grid: Grid,
        groups: Optional[int] = None,
        params: Optional[Union[bool, Tensor, Callable[..., Tensor]]] = True,
        stride: Optional[Union[int, Sequence[int]]] = None,
    ) -> None:
        r"""Initialize transformation parameters.

        Args:
            grid: Grid domain on which transformation is defined.
            groups: Number of transformations. A given image batch can either be deformed by a
                single transformation, or a separate transformation for each image in the batch, e.g.,
                for group-wise or batched registration. The default is one transformation for all images
                in the batch, or the batch length of the ``params`` tensor if provided.
            params: Initial parameters. If a tensor is given, it is only registered as optimizable module
                parameters when of type ``torch.nn.Parameter``. When a callable is given instead, it will be
                called by ``self.update()`` with arguments set and given by ``self.condition()``. When a boolean
                argument is given, a new zero-initialized tensor is created. If ``True``, this tensor is registered
                as optimizable module parameter.
            stride: Number of grid points between control points (minus one). This is the stride of the
                transposed convolution used to upsample the control point displacements to the sampling ``grid``
                size. If ``None``, a stride of 1 is used. If a sequence of values is given, these must be the
                strides for the different spatial grid dimensions in the order ``(sx, sy, sz)``. Note that
                when the control point grid is subdivided in order to double its size along each spatial
                dimension, the stride with respect to this subdivided control point grid remains the same.

        """
        if not grid.align_corners():
            raise ValueError("BSplineTransform() requires 'grid.align_corners() == True'")
        if stride is None:
            stride = 5
        if isinstance(stride, int):
            stride = (stride,) * grid.ndim
        if len(stride) != grid.ndim:
            raise ValueError(f"BSplineTransform() 'stride' must be single int or {grid.ndim} ints")
        self.stride = tuple(reversed([int(s) for s in stride]))
        if groups is None:
            groups = params.shape[0] if isinstance(params, Tensor) else 1
        super().__init__(grid, groups=groups, params=params)
        shape = (groups,) + self.data_shape
        self.register_buffer("u", torch.zeros(shape), persistent=False)
        self.register_kernels(stride)

    @property
    def data_shape(self) -> torch.Size:
        r"""Get shape of transformation parameters tensor."""
        grid = self.grid()
        shape = tuple([math.ceil((n - 1) / s) + 3 for n, s in zip(grid.shape, self.stride)])
        return (grid.ndim,) + shape

    @torch.no_grad()
    def reset_parameters(self) -> None:
        r"""Reset transformation parameters."""
        super().reset_parameters()
        u = getattr(self, "u", None)
        if u is not None:
            init.constant_(u, 0)

    @torch.no_grad()
    def grid_(self: TBSplineTransform, grid: Grid) -> TBSplineTransform:
        r"""Set sampling grid of transformation domain and codomain.

        If ``self.params`` is a callable, only the grid attribute is updated, and
        the callable must return a tensor of matching size upon next evaluation.

        Args:
            grid: New sampling grid for dense displacement field at which FFD is evaluated.
                This function currently only supports subdivision of the control point grid,
                i.e., the new ``grid`` must be have size ``2 * n - 1`` along each spatial
                dimension that should be subdivided, where ``n`` is the current grid size,
                or have the same size as the current grid for dimensions that remain the same.

        Returns:
            Reference to this modified transformation object.

        """
        params = self.params
        if isinstance(params, Tensor):
            current_grid = self._grid
            if grid.ndim != current_grid.ndim:
                raise ValueError(
                    f"{type(self).__name__}.grid_() argument must have {current_grid.ndim} dimensions"
                )
            if not grid.align_corners():
                raise ValueError("BSplineTransform() requires 'grid.align_corners() == True'")
            if not grid.same_domain_as(current_grid):
                raise ValueError(
                    f"{type(self).__name__}.grid_() argument must define same grid domain as current grid"
                )
            subdivide_dims = []
            for i in range(grid.ndim):
                if grid.shape[i] == 2 * current_grid.shape[i] - 1:
                    subdivide_dims.append(2 + i)
                elif grid.shape[i] != current_grid.shape[i]:
                    raise ValueError(
                        f"{type(self).__name__}.grid_() argument must have same size or new size '2n - 1'"
                    )
            if subdivide_dims:
                self.data_(U.subdivide_cubic_bspline(params, dims=subdivide_dims))
        super().grid_(grid)
        return self

    @staticmethod
    def kernel_name(stride: int) -> str:
        r"""Get name of buffer for 1-dimensional kernel for given control point spacing."""
        return "kernel_stride_" + str(stride)

    def kernel(self, stride: int) -> Tensor:
        r"""Get 1-dimensional kernel for given control point spacing."""
        name = self.kernel_name(stride)
        kernel = getattr(self, name)
        return kernel

    def register_kernels(self, stride: Union[int, Sequence[int]]) -> None:
        r"""Precompute cubic B-spline kernels."""
        if isinstance(stride, int):
            stride = [stride]
        for s in stride:
            name = self.kernel_name(s)
            if not hasattr(self, name):
                self.register_buffer(name, K.cubic_bspline1d(s))

    def deregister_kernels(self, stride: Union[int, Sequence[int]]) -> None:
        r"""Remove precomputed cubic B-spline kernels."""
        if isinstance(stride, int):
            stride = [stride]
        for s in stride:
            name = self.kernel_name(s)
            if hasattr(self, name):
                delattr(self, name)

    def evaluate_spline(self) -> Tensor:
        r"""Evaluate cubic B-spline at sampling grid points."""
        data = self.data()
        grid = self.grid()
        stride = self.stride
        if not grid.align_corners():
            raise AssertionError(
                f"{type(self).__name__}() requires grid.align_corners() to be True"
            )
        u = U.conv(
            data,
            kernel=[self.kernel(s) for s in stride],
            stride=stride,
            padding=PaddingMode.ZEROS,
            transpose=True,
        )
        i = (slice(0, u.shape[0]), slice(0, u.shape[1])) + tuple(
            slice(s, s + grid.shape[i]) for i, s in enumerate(stride)
        )
        u = u[i]
        return u


class FreeFormDeformation(BSplineTransform):
    r"""Cubic B-spline free-form deformation model."""

    def update(self) -> FreeFormDeformation:
        r"""Update buffered displacement vector field."""
        super().update()
        self.u = self.evaluate_spline()
        return self


class StationaryVelocityFreeFormDeformation(BSplineTransform):
    r"""Stationary velocity field based transformation model using cubic B-spline parameterization."""

    def __init__(
        self,
        grid: Grid,
        groups: Optional[int] = None,
        params: Optional[Union[bool, Tensor, Callable[..., Tensor]]] = True,
        stride: Optional[Union[int, Sequence[int]]] = None,
        scale: Optional[float] = None,
        steps: Optional[int] = None,
    ) -> None:
        r"""Initialize transformation parameters.

        Args:
            grid: Grid on which to sample flow field vectors.
            groups: Number of velocity fields.
            params: Initial parameters of cubic B-spline velocity fields of shape ``(N, C, ...X)``.
            stride: Number of grid points between control points (minus one).
            scale: Constant scaling factor of velocity fields.
            steps: Number of scaling and squaring steps.

        """
        align_corners = grid.align_corners()
        super().__init__(grid, groups=groups, params=params, stride=stride)
        self.register_buffer("v", torch.zeros_like(self.u), persistent=False)
        self.exp = ExpFlow(scale=scale, steps=steps, align_corners=align_corners)

    @torch.no_grad()
    def reset_parameters(self) -> None:
        r"""Reset transformation parameters."""
        super().reset_parameters()
        v = getattr(self, "v", None)
        if v is not None:
            init.constant_(v, 0)

    def inverse(
        self, link: bool = False, update_buffers: bool = False
    ) -> StationaryVelocityFreeFormDeformation:
        r"""Get inverse of this transformation.

        Args:
            link: Whether to inverse transformation keeps a reference to this transformation.
                If ``True``, the ``update()`` function of the inverse function will not recompute
                shared parameters, e.g., parameters obtained by a callable neural network, but
                directly access the parameters from this transformation. Note that when ``False``,
                the inverse transformation will still share parameters, modules, and buffers with
                this transformation, but these shared tensors may be replaced by a call of ``update()``
                (which is implicitly called as pre-forward hook when ``__call__()`` is invoked).
            update_buffers: Whether buffers of inverse transformation should be update after creating
                the shallow copy. If ``False``, the ``update()`` function of the returned inverse
                transformation has to be called before it is used.

        Returns:
            Shallow copy of this transformation with ``exp`` module which uses negative scaling factor
            to scale and square the stationary velocity field to computes the inverse displacement field.

        """
        inv = shallow_copy(self)
        if link:
            inv.link_(self)
        inv.exp = cast(ExpFlow, self.exp).inverse()
        if update_buffers:
            inv.u = inv.exp(inv.v)
        return inv

    def update(self) -> StationaryVelocityFreeFormDeformation:
        r"""Update buffered velocity and displacement vector fields."""
        super().update()
        self.v = self.evaluate_spline()
        self.u = self.exp(self.v)
        return self
