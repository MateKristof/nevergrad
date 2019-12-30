# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import numpy as np
from . import core


def test_powersystem() -> None:
    func = core.PowerSystem()
    x = 7 * np.random.rand(func.parameter.dimension)
    value = func.function(x)  # should not touch boundaries, so value should be < np.inf
    assert value < np.inf
