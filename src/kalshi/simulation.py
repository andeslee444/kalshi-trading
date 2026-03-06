"""Importance sampling engine for tail-risk contract pricing.

Provides variance-reduced Monte Carlo estimation for computing probabilities
of rare events. Standard Monte Carlo is inefficient for pricing far-OTM
contracts (e.g., P(BTC > $150K tomorrow) ≈ 0.01%) because almost all
samples fall in the non-event region.

Importance sampling shifts the sampling distribution toward the tail,
then corrects with importance weights, giving much lower variance
for the same number of samples.

Usage:
    from simulation import importance_sample_probability

    prob, std_err = importance_sample_probability(
        mean=log_return_mean, std=log_return_std,
        threshold=log(K/S), n_samples=10000,
    )
"""

import math
import random

from probability import _norm_cdf, _norm_pdf


def importance_sample_probability(mean, std, threshold, n_samples=10000,
                                   direction="above", use_antithetic=False):
    """Compute tail probability using importance sampling.

    Uses exponential tilting: shifts the Gaussian mean toward the
    threshold to sample more tail events, then corrects with
    importance weights.

    Args:
        mean: Distribution mean.
        std: Distribution standard deviation.
        threshold: Event threshold.
        n_samples: Number of Monte Carlo samples.
        direction: "above" for P(X > threshold), "below" for P(X < threshold).
        use_antithetic: If True, use antithetic variates for variance reduction.

    Returns:
        (probability, standard_error) tuple.
    """
    if std <= 0:
        if direction == "above":
            return (1.0, 0.0) if mean > threshold else (0.0, 0.0)
        else:
            return (1.0, 0.0) if mean < threshold else (0.0, 0.0)

    # Optimal shift: move mean to threshold for maximum efficiency
    # Proposal distribution: N(shift_mean, std)
    shift_mean = threshold  # Shift to threshold

    weights = []
    indicators = []

    actual_n = n_samples // 2 if use_antithetic else n_samples

    for _ in range(actual_n):
        # Sample from proposal N(shift_mean, std)
        z = random.gauss(0, 1)
        samples = [shift_mean + std * z]

        if use_antithetic:
            # Antithetic variate: use -z as well
            samples.append(shift_mean + std * (-z))

        for x in samples:
            # Importance weight: p(x) / q(x) where p=N(mean,std), q=N(shift_mean,std)
            # log(w) = -0.5*((x-mean)/std)^2 + 0.5*((x-shift_mean)/std)^2
            log_w = -0.5 * ((x - mean) / std) ** 2 + 0.5 * ((x - shift_mean) / std) ** 2
            w = math.exp(log_w)

            # Indicator function
            if direction == "above":
                ind = 1.0 if x > threshold else 0.0
            else:
                ind = 1.0 if x < threshold else 0.0

            weights.append(w * ind)
            indicators.append(ind)

    n = len(weights)
    if n == 0:
        return (0.0, 0.0)

    # Weighted estimate
    prob = sum(weights) / n

    # Standard error via sample variance of weighted indicator (iid approximation)
    mean_w = prob
    var_w = sum((w - mean_w) ** 2 for w in weights) / (n - 1) if n > 1 else 0.0
    std_err = math.sqrt(var_w / n) if var_w > 0 else 0.0
    std_err = max(std_err, 1.0 / n)  # Floor: at least 1/n uncertainty

    return (max(0.0, min(1.0, prob)), std_err)


def naive_mc_probability(mean, std, threshold, n_samples=10000,
                          direction="above"):
    """Standard (naive) Monte Carlo probability estimation.

    Used as baseline for comparing against importance sampling.

    Returns:
        (probability, standard_error) tuple.
    """
    if std <= 0:
        if direction == "above":
            return (1.0, 0.0) if mean > threshold else (0.0, 0.0)
        else:
            return (1.0, 0.0) if mean < threshold else (0.0, 0.0)

    count = 0
    for _ in range(n_samples):
        x = random.gauss(mean, std)
        if direction == "above":
            if x > threshold:
                count += 1
        else:
            if x < threshold:
                count += 1

    prob = count / n_samples
    # Binomial standard error
    std_err = math.sqrt(prob * (1 - prob) / n_samples) if n_samples > 0 else 0.0

    return (prob, std_err)
