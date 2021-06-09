r"""Defines composite spatial transformations."""

from __future__ import annotations

from copy import copy as shallow_copy
from collections import OrderedDict
from typing import Iterable, Optional, Tuple, TypeVar, Union, overload

import torch
from torch import Tensor
from torch.nn import ModuleDict

from ...core.grid import Axes, Grid, grid_transform_points
from ...core.linalg import as_homogeneous_matrix, homogeneous_matmul
from ...core.tensor import move_dim

from .base import SpatialTransform


TCompositeTransform = TypeVar("TCompositeTransform", bound="CompositeTransform")


class CompositeTransform(SpatialTransform):
    r"""Base class of composite spatial coordinate transformations.

    Base class of modules that apply one or more spatial transformations to map a tensor of
    spatial points to another tensor of spatial points of the same shape as the input tensor.

    """

    @overload
    def __init__(self, grid: Grid) -> None:
        r"""Initialize empty composite transformation."""
        ...

    @overload
    def __init__(self, grid: Grid, *args: Optional[SpatialTransform]) -> None:
        r"""Initialize composite transformation."""
        ...

    @overload
    def __init__(self, grid: Grid, transforms: Union[OrderedDict, ModuleDict]) -> None:
        r"""Initialize composite transformation given named transforms in ordered dictionary."""
        ...

    @overload
    def __init__(self, *args: Optional[SpatialTransform]) -> None:
        r"""Initialize composite transformation."""
        ...

    @overload
    def __init__(self, transforms: Union[ModuleDict, OrderedDict]) -> None:
        r"""Initialize composite transformation given named transforms in ordered dictionary."""
        ...

    def __init__(
        self, *args: Optional[Union[Grid, ModuleDict, OrderedDict, SpatialTransform]]
    ) -> None:
        r"""Initialize composite transformation."""
        args_ = [arg for arg in args if arg is not None]
        grid = None
        if isinstance(args_[0], Grid):
            grid = args_[0]
            args_ = args_[1:]
        if args_:
            if isinstance(args_[0], (dict, ModuleDict)):
                if len(args_) > 1:
                    raise ValueError(
                        f"{type(self).__name__}() multiple arguments not allowed when dict is given"
                    )
                transforms = args_[0]
            else:
                transforms = OrderedDict([(str(i), t) for i, t in enumerate(args_)])
        else:
            transforms = OrderedDict()
        if grid is None:
            if transforms:
                transform = next(iter(transforms.values()))
                grid = transform.grid()
            else:
                raise ValueError(
                    f"{type(self).__name__}() requires a Grid or at least one SpatialTransform"
                )
        for name, transform in transforms.items():
            if not isinstance(transform, SpatialTransform):
                raise TypeError(
                    f"{type(self).__name__}() module '{name}' must be of type SpatialTransform"
                )
            if not transform.grid().same_domain_as(grid):
                raise ValueError(
                    f"{type(self).__name__}() transform '{name}' has different 'grid' center, direction, or cube extent"
                )
        super().__init__(grid)
        self._transforms = ModuleDict(transforms)

    def bool(self) -> bool:
        r"""Whether this module has at least one transformation."""
        return len(self._transforms) > 0

    def __len__(self) -> int:
        r"""Number of spatial transformations."""
        return len(self._transforms)

    @property
    def linear(self) -> bool:
        r"""Whether composite transformation is linear."""
        return all(transform.linear for transform in self.transforms())

    def __contains__(self, name: Union[int, str]) -> bool:
        r"""Whether composite contains named transformation."""
        if isinstance(name, int):
            name = str(name)
        return name in self._transforms.keys()

    def __getitem__(self, name: Union[int, str]) -> SpatialTransform:
        r"""Get named transformation."""
        if isinstance(name, int):
            name = str(name)
        return self._transforms[name]

    def get(
        self, name: Union[int, str], default: Optional[SpatialTransform] = None
    ) -> Optional[SpatialTransform]:
        r"""Get named transformation."""
        if isinstance(name, int):
            name = str(name)
        for key, transform in self._transforms.items():
            if key == name:
                assert isinstance(transform, SpatialTransform)
                return transform
        return default

    def transforms(self) -> Iterable[SpatialTransform]:
        r"""Iterate transformations in order of composition."""
        return self._transforms.values()

    def named_transforms(self) -> Iterable[Tuple[str, SpatialTransform]]:
        r"""Iterate transformations in order of composition."""
        return self._transforms.items()

    def condition(
        self: TCompositeTransform, *args, **kwargs
    ) -> Union[TCompositeTransform, Optional[Tensor]]:
        r"""Get or set data tensor on which transformations are conditioned."""
        if args or kwargs:
            return shallow_copy(self).condition_(*args, **kwargs)
        return self._args, self._kwargs

    def condition_(self: TCompositeTransform, *args, **kwargs) -> TCompositeTransform:
        r"""Set data tensor on which transformations are conditioned."""
        assert args or kwargs
        super().condition_(*args, **kwargs)
        for transform in self.transforms():
            transform.condition_(*args, **kwargs)
        return self

    def disp(self, grid: Optional[Grid] = None) -> Tensor:
        r"""Get displacement vector field representation of this transformation.

        Args:
            grid: Grid on which to sample vector fields. Use ``self.grid()`` if ``None``.

        Returns:
            Displacement vector fields as tensor of shape ``(N, D, ..., X)``.

        """
        if grid is None:
            grid = self.grid()
        axes = Axes.from_grid(grid)
        x = grid.coords(device=self.device).unsqueeze(0)
        if grid.same_domain_as(self.grid()):
            y = self.forward(x)
        else:
            y = grid_transform_points(x, grid, axes, self.grid(), self.axes())
            y = self.forward(y)
            y = grid_transform_points(y, self.grid(), self.axes(), grid, axes)
        u = y - x
        u = move_dim(u, -1, 1)
        return u

    def update(self: TCompositeTransform) -> TCompositeTransform:
        r"""Update buffered data such as predicted parameters, velocities, and/or displacements."""
        super().update()
        for transform in self.transforms():
            transform.update()
        return self


class MultiLevelTransform(CompositeTransform):
    r"""Sum of spatial transformations applied to any set of points."""

    def forward(self, points: Tensor, grid: bool = False) -> Tensor:
        r"""Transform set of points by sum of spatial transformations.

        Args:
            points: Tensor of shape ``(N, M, D)`` or ``(N, ..., Y, X, D)``.
            grid: Whether ``points`` are the positions of undeformed grid points.

        Returns:
            Tensor of same shape as ``points`` with transformed point coordinates.

        """
        x = points
        if len(self) == 0:
            return x
        if self.linear:
            # The base class implementation uses composite self.tensor(), which is
            # more efficient in case of the composition of linear transformations.
            y = super().forward(points, grid)
        else:
            u = torch.zeros_like(x)
            for i, transform in enumerate(self.transforms()):
                y = transform.forward(x, grid=grid and i == 0)
                u += y - x
            y = x + u
        return y

    def tensor(self) -> Tensor:
        r"""Get tensor representation of this transformation.

        The tensor representation of a transformation is with respect to the unit cube axes defined
        by its sampling grid as specified by ``self.axes()``.

        Returns:
            In case of a composition of linear transformations, returns a batch of homogeneous transformation
            matrices as tensor of shape ``(N, D, 1)`` (translation),  ``(N, D, D)`` (affine) or ``(N, D, D + 1)``,
            i.e., a 3-dimensional tensor. If this composite transformation contains a non-rigid transformation,
            a displacement vector field is returned as tensor of shape ``(N, D, ..., X)``.

        """
        if self.linear:
            transforms = list(self.transforms())
            if not transforms:
                identity = torch.eye(self.ndim, self.ndim + 1, device=self.device)
                return identity.unsqueeze(0)
            transform = transforms[0]
            mat = as_homogeneous_matrix(transform.tensor())
            for transform in transforms[1:]:
                mat += as_homogeneous_matrix(transform.tensor())
            return mat
        return self.disp()


class SequentialTransform(CompositeTransform):
    r"""Composition of spatial transformations applied to any set of points."""

    def forward(self, points: Tensor, grid: bool = False) -> Tensor:
        r"""Transform points by sequence of spatial transformations.

        Args:
            points: Tensor of shape ``(N, M, D)`` or ``(N, ..., Y, X, D)``.
            grid: Whether ``points`` are the positions of undeformed grid points.

        Returns:
            Tensor of same shape as ``points`` with transformed point coordinates.

        """
        # The base class implementation uses self.tensor(), which is more efficient
        # in case of the composition of only linear transformations.
        if self.linear:
            return super().forward(points, grid)
        y = points
        for i, transform in enumerate(self.transforms()):
            y = transform.forward(y, grid=grid and i == 0)
        return y

    def tensor(self) -> Tensor:
        r"""Get tensor representation of this transformation.

        The tensor representation of a transformation is with respect to the unit cube axes defined
        by its sampling grid as specified by ``self.axes()``.

        Returns:
            In case of a composition of linear transformations, returns a batch of homogeneous transformation
            matrices as tensor of shape ``(N, D, 1)`` (translation),  ``(N, D, D)`` (affine) or ``(N, D, D + 1)``,
            i.e., a 3-dimensional tensor. If this composite transformation contains a non-rigid transformation,
            a displacement vector field is returned as tensor of shape ``(N, D, ..., X)``.

        """
        if self.linear:
            transforms = list(self.transforms())
            if not transforms:
                identity = torch.eye(self.ndim, self.ndim + 1, device=self.device)
                return identity.unsqueeze(0)
            transform = transforms[0]
            mat = transform.tensor()
            for transform in transforms[1:]:
                mat = homogeneous_matmul(transform.tensor(), mat)
            return mat
        return self.disp()

    def inverse(
        self: TCompositeTransform, link: bool = False, update_buffers: bool = False
    ) -> TCompositeTransform:
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
            Shallow copy of this transformation which computes and applied the inverse transformation.
            The inverse transformation will share the parameters with this transformation. Not all
            transformations may implement this functionality.

        Raises:
            NotImplementedError: When a transformation does not support sharing parameters with its inverse.

        """
        copy = shallow_copy(self)
        transforms = ModuleDict()
        for name, transform in reversed(self.named_transforms()):
            assert isinstance(transform, SpatialTransform)
            transforms[name] = transform.inverse(link=link, update_buffers=update_buffers)
        copy._transforms = transforms
        return copy
