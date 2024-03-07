from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from optuna._gp.acqf import AcquisitionFunctionParams
from optuna._gp.acqf import eval_acqf_no_grad
from optuna._gp.acqf import eval_acqf_with_grad
from optuna._gp.search_space import normalize_one_param
from optuna._gp.search_space import sample_normalized_params
from optuna._gp.search_space import ScaleType
from optuna.logging import get_logger


if TYPE_CHECKING:
    import scipy.optimize as so
else:
    from optuna import _LazyImport

    so = _LazyImport("scipy.optimize")

_logger = get_logger(__name__)


def _gradient_ascent(
    acqf_params: AcquisitionFunctionParams,
    initial_params: np.ndarray,
    initial_fval: float,
    continuous_indices: np.ndarray,
    lengthscale: np.ndarray,
    tol: float,
) -> tuple[np.ndarray, float, bool]:
    """
    This function optimizes the acquisition function using preconditioning.
    Preconditioning equalizes the variances caused by each parameter and
    speeds up the convergence.

    In Optuna, acquisition functions use Matern 5/2 kernel, which is a function of `x / l`
    where `x` is `normalized_params` and `l` is the corresponding lengthscales.
    Then acquisition functions are a function of `x / l`, i.e. `f(x / l)`.
    As `l` has different values for each param, it makes the function ill-conditioned.
    By transforming `x / l` to `zl / l = z`, the function becomes `f(z)` and has
    equal variances w.r.t. `z`.
    So optimization w.r.t. `z` instead of `x` is the preconditioning here and
    speeds up the convergence.
    As the domain of `x` is [0, 1], that of `z` becomes [0, 1/l].
    """
    if len(continuous_indices) == 0:
        return (initial_params, initial_fval, False)
    normalized_params = initial_params.copy()

    def negative_acqf_with_grad(scaled_x: np.ndarray) -> tuple[float, np.ndarray]:
        # Scale back to the original domain, i.e. [0, 1], from [0, 1/s].
        normalized_params[continuous_indices] = scaled_x * lengthscale
        (fval, grad) = eval_acqf_with_grad(acqf_params, normalized_params)
        # Flip sign because scipy minimizes functions.
        # Let the scaled acqf be g(x) and the acqf be f(sx), then dg/dx = df/dx * s.
        return (-fval, -grad[continuous_indices] * lengthscale)

    scaled_cont_x_opt, neg_fval_opt, info = so.fmin_l_bfgs_b(
        func=negative_acqf_with_grad,
        x0=normalized_params[continuous_indices] / lengthscale,
        bounds=[(0, 1 / s) for s in lengthscale],
        pgtol=math.sqrt(tol),
        maxiter=200,
    )

    if -neg_fval_opt > initial_fval and info["nit"] > 0:  # Improved.
        # `nit` is the number of iterations.
        normalized_params[continuous_indices] = scaled_cont_x_opt * lengthscale
        return (normalized_params, -neg_fval_opt, True)

    return (initial_params, initial_fval, False)  # No improvement.


def _exhaustive_search(
    acqf_params: AcquisitionFunctionParams,
    initial_params: np.ndarray,
    initial_fval: float,
    param_idx: int,
    choices: np.ndarray,
) -> tuple[np.ndarray, float, bool]:
    choices_except_current = choices[choices != initial_params[param_idx]]

    all_params = np.repeat(initial_params[None, :], len(choices_except_current), axis=0)
    all_params[:, param_idx] = choices_except_current
    fvals = eval_acqf_no_grad(acqf_params, all_params)
    best_idx = np.argmax(fvals)

    if fvals[best_idx] > initial_fval:  # Improved.
        return (all_params[best_idx, :], fvals[best_idx], True)

    return (initial_params, initial_fval, False)  # No improvement.


def _discrete_line_search(
    acqf_params: AcquisitionFunctionParams,
    initial_params: np.ndarray,
    initial_fval: float,
    param_idx: int,
    grids: np.ndarray,
    xtol: float,
) -> tuple[np.ndarray, float, bool]:
    if len(grids) == 1:
        # Do not optimize anything when there's only one choice.
        return (initial_params, initial_fval, False)

    def find_nearest_index(x: float) -> int:
        i = int(np.clip(np.searchsorted(grids, x), 1, len(grids) - 1))
        return i - 1 if abs(x - grids[i - 1]) < abs(x - grids[i]) else i

    current_choice_i = find_nearest_index(initial_params[param_idx])
    assert initial_params[param_idx] == grids[current_choice_i]

    negative_fval_cache = {current_choice_i: -initial_fval}

    normalized_params = initial_params.copy()

    def negative_acqf_with_cache(i: int) -> float:
        # Function value at choices[i].
        cache_val = negative_fval_cache.get(i)
        if cache_val is not None:
            return cache_val
        normalized_params[param_idx] = grids[i]

        # Flip sign because scipy minimizes functions.
        negval = -float(eval_acqf_no_grad(acqf_params, normalized_params))
        negative_fval_cache[i] = negval
        return negval

    def interpolated_negative_acqf(x: float) -> float:
        if x < grids[0] or x > grids[-1]:
            return np.inf
        right = int(np.clip(np.searchsorted(grids, x), 1, len(grids) - 1))
        left = right - 1
        neg_acqf_left, neg_acqf_right = negative_acqf_with_cache(left), negative_acqf_with_cache(
            right
        )
        w_left = (grids[right] - x) / (grids[right] - grids[left])
        w_right = 1.0 - w_left
        return w_left * neg_acqf_left + w_right * neg_acqf_right

    EPS = 1e-12
    res = so.minimize_scalar(
        interpolated_negative_acqf,
        # The values of this bracket are (inf, -fval, inf).
        # This trivially satisfies the bracket condition if fval is finite.
        bracket=(grids[0] - EPS, grids[current_choice_i], grids[-1] + EPS),
        method="brent",
        tol=xtol,
    )
    opt_idx = find_nearest_index(res.x)
    fval_opt = -negative_acqf_with_cache(opt_idx)

    # We check both conditions because of numerical errors.
    if opt_idx != current_choice_i and fval_opt > initial_fval:
        normalized_params[param_idx] = grids[opt_idx]
        return (normalized_params, fval_opt, True)
    else:
        return (initial_params, initial_fval, False)


def _local_search_discrete(
    acqf_params: AcquisitionFunctionParams,
    initial_params: np.ndarray,
    initial_fval: float,
    param_idx: int,
    choices: np.ndarray,
    xtol: float,
) -> tuple[np.ndarray, float, bool]:

    # If the number of possible parameter values is small, we just perform an exhaustive search.
    # This is faster and better than the line search.
    MAX_INT_EXHAUSTIVE_SEARCH_PARAMS = 16

    scale_type = acqf_params.search_space.scale_types[param_idx]
    if scale_type == ScaleType.CATEGORICAL or len(choices) <= MAX_INT_EXHAUSTIVE_SEARCH_PARAMS:
        return _exhaustive_search(acqf_params, initial_params, initial_fval, param_idx, choices)
    else:
        return _discrete_line_search(
            acqf_params, initial_params, initial_fval, param_idx, choices, xtol
        )


def local_search_mixed(
    acqf_params: AcquisitionFunctionParams,
    initial_normalized_params: np.ndarray,
    *,
    tol: float = 1e-4,
    max_iter: int = 100,
) -> tuple[np.ndarray, float]:
    scale_types = acqf_params.search_space.scale_types
    bounds = acqf_params.search_space.bounds
    steps = acqf_params.search_space.steps

    continuous_indices = np.where(steps == 0.0)[0]

    inverse_squared_lengthscales = (
        acqf_params.kernel_params.inverse_squared_lengthscales.detach().numpy()
    )
    # This is a technique for speeding up optimization.
    # We use an isotropic kernel, so scaling the gradient will make
    # the hessian better-conditioned.
    lengthscale = 1 / np.sqrt(inverse_squared_lengthscales[continuous_indices])

    discrete_indices = np.where(steps > 0)[0]
    choices_of_discrete_params = [
        (
            np.arange(bounds[i, 1])
            if scale_types[i] == ScaleType.CATEGORICAL
            else normalize_one_param(
                param_value=np.arange(bounds[i, 0], bounds[i, 1] + 0.5 * steps[i], steps[i]),
                scale_type=ScaleType(scale_types[i]),
                bounds=(bounds[i, 0], bounds[i, 1]),
                step=steps[i],
            )
        )
        for i in discrete_indices
    ]

    noncontinuous_paramwise_xtol = [
        # Terminate discrete optimizations once the change in x becomes smaller than this.
        # Basically, if the change is smaller than min(dx) / 4, it is useless to see more details.
        np.min(np.diff(choices), initial=np.inf) / 4
        for choices in choices_of_discrete_params
    ]

    best_normalized_params = initial_normalized_params.copy()
    best_fval = float(eval_acqf_no_grad(acqf_params, best_normalized_params))

    CONTINUOUS = -1
    last_changed_param: int | None = None

    for _ in range(max_iter):
        if last_changed_param == CONTINUOUS:
            # Parameters not changed since last time.
            return (best_normalized_params, best_fval)
        (best_normalized_params, best_fval, updated) = _gradient_ascent(
            acqf_params,
            best_normalized_params,
            best_fval,
            continuous_indices,
            lengthscale,
            tol,
        )
        if updated:
            last_changed_param = CONTINUOUS

        for i, choices, xtol in zip(
            discrete_indices, choices_of_discrete_params, noncontinuous_paramwise_xtol
        ):
            if last_changed_param == i:
                # Parameters not changed since last time.
                return (best_normalized_params, best_fval)
            (best_normalized_params, best_fval, updated) = _local_search_discrete(
                acqf_params, best_normalized_params, best_fval, i, choices, xtol
            )
            if updated:
                last_changed_param = i

        if last_changed_param is None:
            # Parameters not changed from the beginning.
            return (best_normalized_params, best_fval)

    _logger.warn("local_search_mixed: Local search did not converge.")
    return (best_normalized_params, best_fval)


def optimize_acqf_mixed(
    acqf_params: AcquisitionFunctionParams,
    *,
    given_initial_xs: np.ndarray | None = None,
    n_additional_samples: int = 2048,
    n_local_search: int = 10,
    tol: float = 1e-4,
    rng: np.random.RandomState | None = None,
) -> tuple[np.ndarray, float]:

    rng = rng or np.random.RandomState()

    dim = acqf_params.search_space.scale_types.shape[0]
    if given_initial_xs is None:
        given_initial_xs = np.empty((0, dim))

    assert (
        len(given_initial_xs) <= n_local_search - 1
    ), "We must choose at least 1 best sampled point + given_initial_xs as start points."

    sampled_xs = sample_normalized_params(n_additional_samples, acqf_params.search_space, rng=rng)

    # Evaluate all values at initial samples
    f_vals = eval_acqf_no_grad(acqf_params, sampled_xs)
    assert isinstance(f_vals, np.ndarray)

    max_i = np.argmax(f_vals)

    probs = np.exp(f_vals - f_vals[max_i])
    probs[max_i] = 0.0
    probs /= probs.sum()
    remaining_idxs = rng.choice(
        len(sampled_xs),
        size=n_local_search - len(given_initial_xs) - 1,
        replace=False,
        p=probs,
    )

    best_x = sampled_xs[max_i, :]
    best_f = float(f_vals[max_i])

    for x_guess in np.vstack(
        [sampled_xs[max_i, :], sampled_xs[remaining_idxs, :], given_initial_xs]
    ):
        x, f = local_search_mixed(acqf_params, x_guess, tol=tol)
        if f > best_f:
            best_x = x
            best_f = f

    return best_x, best_f
