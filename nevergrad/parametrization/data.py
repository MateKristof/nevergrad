# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import warnings
import typing as t
import numpy as np
from .core import Parameter
from .core import _as_parameter
from ..instrumentation import transforms as trans  # TODO move along


BoundValue = t.Optional[t.Union[float, int, np.int, np.float, np.ndarray]]
A = t.TypeVar("A", bound="Array")


class BoundChecker:
    """Simple object for checking whether an array lies
    between provided bounds.

    Parameter
    ---------
    a_min: float or None
        minimum value
    a_max: float or None
        maximum value

    Note
    -----
    Not all bounds are necessary (data can be partially bounded, or not at all actually)
    """

    def __init__(self, a_min: BoundValue = None, a_max: BoundValue = None) -> None:
        self.bounds = (a_min, a_max)

    def __call__(self, value: np.ndarray) -> bool:
        """Checks whether the array lies within the bounds

        Parameter
        ---------
        value: np.ndarray
            array to check

        Returns
        -------
        bool
            True iff the array lies within the bounds
        """
        for k, bound in enumerate(self.bounds):
            if bound is not None:
                if np.any((value > bound) if k else (value < bound)):
                    return False
        return True


# pylint: disable=too-many-arguments
class Array(Parameter):
    """Array variable of a given shape.

    Parameters
    ----------
    init: np.ndarray, or None
        initial value of the array (defaults to 0, with a provided shape)
    shape: tuple of ints, or None
        shape of the array, to be provided iff init is not provided
    mutable_sigma: bool
        whether the mutation standard deviation must mutate as well (for mutation based algorithms)

    Note
    ----
    More specific behaviors can be obtained throught the following methods:
    set_bounds, set_mutation, set_integer_casting
    """

    def __init__(
            self,
            *,
            init: t.Optional[np.ndarray] = None,
            shape: t.Optional[t.Tuple[int, ...]] = None,
            mutable_sigma: bool = False
    ) -> None:
        sigma = Log(init=1.0, exponent=1.2, mutable_sigma=False) if mutable_sigma else 1.0
        super().__init__(sigma=sigma, recombination="average")
        err_msg = 'Exactly one of "init" or "shape" must be provided'
        if init is not None:
            if shape is not None:
                raise ValueError(err_msg)
            assert isinstance(init, np.ndarray)
            self._value: np.ndarray = init
        elif shape is not None:
            assert isinstance(shape, tuple) and all(isinstance(n, int) for n in shape)
            self._value = np.zeros(shape)
        else:
            raise ValueError(err_msg)
        self.integer = False
        self.exponent: t.Optional[float] = None
        self.bounds: t.Tuple[t.Optional[np.ndarray], t.Optional[np.ndarray]] = (None, None)
        self.bound_transform: t.Optional[trans.BoundTransform] = None
        self.full_range_sampling = False

    @property
    def sigma(self) -> t.Union[np.ndarray, float]:
        """Value for the standard deviation used to mutate the parameter
        """
        return _as_parameter(self._subparameters["sigma"]).value  # type: ignore

    @property
    def value(self) -> np.ndarray:
        if self.integer:
            return np.round(self._value)  # type: ignore
        return self._value

    @value.setter
    def value(self, value: np.ndarray) -> None:
        if not isinstance(value, np.ndarray):
            raise TypeError(f"Received a {type(value)} in place of a np.ndarray")
        if self._value.shape != value.shape:
            raise ValueError(f"Cannot set array of shape {self._value.shape} with value of shape {value.shape}")
        if not BoundChecker(*self.bounds)(self.value):
            raise ValueError("New value does not comply with bounds")
        if self.exponent is not None and np.min(value.ravel()) <= 0:
            raise ValueError("Logirithmic values cannot be negative")
        self._value = value

    def sample(self: A) -> A:
        if not self.full_range_sampling:
            return super().sample()
        child = self.spawn_child()
        std_bounds = tuple(self._to_std_space(b) for b in self.bounds)  # type: ignore
        diff = std_bounds[1] - std_bounds[0]
        child.set_std_data(std_bounds[0] + np.random.uniform(0, 1, size=diff.shape) * diff, deterministic=False)
        return child

    def set_bounds(self: A, a_min: BoundValue = None, a_max: BoundValue = None,
                   method: str = "clipping", full_range_sampling: bool = False) -> A:
        """Bounds all real values into [a_min, a_max] using a provided method

        Parameters
        ----------
        a_min: float or None
            minimum value
        a_max: float or None
            maximum value
        method: str
            "clipping", "constraint", "tanh" or "arctan"
        full_range_sampling: bool
            whether calling the "sample" method of the parameter should sample uniformly (or log-uniformly) on the whole
            range of the bounds instead of sampling using a mutation on the current value

        Notes
        -----
        - "tanh" reaches the boundaries really quickly, while "arctan" is much softer
        - only "clipping" accepts partial bounds (None values)
        """  # TODO improve description of methods
        bounds = tuple(a if isinstance(a, np.ndarray) or a is None else np.array([a], dtype=float) for a in (a_min, a_max))
        both_bounds = all(b is not None for b in bounds)
        # preliminary checks
        if self.bound_transform is not None:
            raise RuntimeError("A bounding method has already been set")
        if full_range_sampling and not both_bounds:
            raise ValueError("Cannot use full range sampling if both bounds are not set")
        checker = BoundChecker(*bounds)
        if not checker(self.value):
            raise ValueError("Current value is not within bounds, please update it first")
        if not (a_min is None or a_max is None):
            if (bounds[0] >= bounds[1]).any():  # type: ignore
                raise ValueError(f"Lower bounds {a_min} should be strictly smaller than upper bounds {a_max}")
        # update instance
        transforms = dict(clipping=trans.Clipping, arctan=trans.ArctanBound, tanh=trans.TanhBound)
        if method in transforms:
            if self.exponent is not None and method != "clipping":
                raise ValueError(f'Cannot use method "{method}" in logarithmic mode')
            self.bound_transform = transforms[method](*bounds)
        elif method == "constraint":
            self.register_cheap_constraint(checker)
        else:
            raise ValueError(f"Unknown method {method}")
        self.bounds = bounds  # type: ignore
        self.full_range_sampling = full_range_sampling
        # warn if sigma is too large for range
        if both_bounds and method != "tanh":  # tanh goes to infinity anyway
            std_bounds = tuple(self._to_std_space(b) for b in self.bounds)  # type: ignore
            min_dist = np.min(np.abs(std_bounds[0] - std_bounds[1]).ravel())
            if min_dist < 3.0:
                warnings.warn(f"Bounds are {min_dist} sigma away from each other at the closest, "
                              "you should aim for at least 3 for better quality.")
        return self

    def set_mutation(self: A, sigma: t.Optional[t.Union[float, "Array"]] = None, exponent: t.Optional[float] = None) -> A:
        """Output will be cast to integer(s) through deterministic rounding.

        Parameters
        ----------
        sigma: Array/Log or float
            The standard deviation of the mutation. If a Parameter is provided, it will replace the current
            value. If a float is provided, it will either replace a previous float value, or update the value
            of the Parameter.
        exponent: float
            exponent for the logarithmic mode. With the default sigma=1, using exponent=2 will perform
            x2 or /2 "on average" on the value at each mutation.

        Returns
        -------
        self
        """
        if sigma is not None:
            # just replace if an actual Parameter is provided as sigma, else update value (parametrized or not)
            if isinstance(sigma, Parameter) or isinstance(self.subparameters._parameters["sigma"], float):
                self.subparameters._parameters["sigma"] = sigma
            else:
                self.subparameters._parameters["sigma"].value = sigma
        if exponent is not None:
            if self.bound_transform is not None and not self.bound_transform.name.startswith("Cl"):
                raise RuntimeError(f"Cannot set logarithmic transform with bounding transform {self.bound_transform}, "
                                   "only clipping and constraint bounding methods can accept it.")
            if exponent <= 1.0:
                raise ValueError("Only exponents strictly higher than 1.0 are allowed")
            if np.min(self._value.ravel()) <= 0:
                raise RuntimeError("Cannot convert to logarithmic mode with current non-positive value, please update it first.")
            self.exponent = exponent
        return self

    def set_integer_casting(self: A) -> A:
        """Output will be cast to integer(s) through deterministic rounding.

        Returns
        -------
        self
        """
        self.integer = True
        return self

    # pylint: disable=unused-argument
    def _internal_set_std_data(self: A, data: np.ndarray, instance: A, deterministic: bool = False) -> A:
        assert isinstance(data, np.ndarray)
        sigma = self._get_parameter_value("sigma")
        data_reduc = (sigma * data).reshape(instance._value.shape)
        instance._value = data_reduc if self.exponent is None else self.exponent**data_reduc
        if instance.bound_transform is not None:
            instance._value = instance.bound_transform.forward(instance._value)
        return instance

    def _internal_spawn_child(self) -> "Array":
        child = self.__class__(init=self.value)
        child.subparameters._parameters = {k: v.spawn_child() if isinstance(v, Parameter) else v
                                           for k, v in self.subparameters._parameters.items()}
        for name in ["integer", "exponent", "bounds", "bound_transform", "full_range_sampling"]:
            setattr(child, name, getattr(self, name))
        return child

    def _internal_get_std_data(self: A, instance: A) -> np.ndarray:
        return self._to_std_space(instance._value)

    def _to_std_space(self, data: np.ndarray) -> np.ndarray:
        """Converts array with appropriate shapes to the standard space of this instance
        """
        sigma = self._get_parameter_value("sigma")
        if self.bound_transform is not None:
            data = self.bound_transform.backward(data)
        distribval = data if self.exponent is None else np.log(data) / np.log(self.exponent)
        reduced = distribval / sigma
        return reduced.ravel()  # type: ignore

    def recombine(self: A, *others: A) -> None:
        recomb = self._get_parameter_value("recombination")
        all_p = [self] + list(others)
        if recomb == "average":
            self.set_std_data(np.mean([self.get_std_data(p) for p in all_p], axis=0), deterministic=False)
        else:
            raise ValueError(f'Unknown recombination "{recomb}"')


class Scalar(Array):
    """Parameter representing a scalar

    Parameters
    ----------
    init: float
        initial value of the scalar
    mutable_sigma: bool
        whether the mutation standard deviation must mutate as well (for mutation based algorithms)

    Note
    ----
    More specific behaviors can be obtained throught the following methods:
    set_bounds, set_mutation
    """

    def __init__(self, init: float = 0.0, mutable_sigma: bool = True) -> None:
        super().__init__(init=np.array([init]), mutable_sigma=mutable_sigma)

    @property  # type: ignore
    def value(self) -> float:  # type: ignore
        return self._value[0] if not self.integer else int(self._value[0])  # type: ignore

    @value.setter
    def value(self, value: float) -> None:
        if not isinstance(value, (float, int, np.float, np.int)):
            raise TypeError(f"Received a {type(value)} in place of a scalar (float, int)")
        self._value = np.array([value], dtype=float)


class Log(Scalar):
    """Parameter representing a log distributed value between 0 and infinity

    Parameters
    ----------
    init: float
        initial value of the variable
    exponent: float
        exponent for the log mutation: an exponent of 2.0 will lead to mutations by factors between around 0.5 and 2.0
    a_min: float or None
        minimum value if any (> 0)
    a_max: float or None
        maximum value if any
    mutable_sigma: bool
        whether the mutation standard deviation must mutate as well (for mutation based algorithms)

    Note
    ----
    More specific behaviors can be obtained throught the following methods:
    set_bounds, set_mutation
    """

    def __init__(
        self,
        *,
        init: float = 1.0,
        exponent: float = 2.0,
        a_min: t.Optional[float] = None,
        a_max: t.Optional[float] = None,
        mutable_sigma: bool = False,
    ) -> None:
        super().__init__(init=init, mutable_sigma=mutable_sigma)
        self.set_mutation(sigma=1.0, exponent=exponent)
        if any(a is not None for a in (a_min, a_max)):
            self.set_bounds(a_min, a_max, method="clipping")