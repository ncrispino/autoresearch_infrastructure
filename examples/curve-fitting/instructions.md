# Fit the observations more closely

## Objective

Improve `/workspace/candidate/coefficients.json` so that its polynomial fits the
thirteen `(x, y)` observations in `/context/task.json` more closely.

The file holds `{"coefficients": [...]}` in ascending powers: `[c0, c1, c2]`
means `y = c0 + c1*x + c2*x*x`. The baseline is the constant zero, `[0.0]`.

`rmse` is the root mean squared error on those thirteen observations. Fitting
them is the task; there is no hidden test set.

Read the observations and the baseline result first, then compare simple
polynomial fits. Python's standard library is enough to calculate one in `/tmp`.

## Constraints

### Rules

- Submit only `coefficients.json`, containing an object with just the
  `coefficients` key.
- Use one to three finite numbers between -10 and 10: a constant, a straight
  line or a quadratic.
- Keep scripts and scratch files out of the candidate directory.

### Environment

- The observations are read-only. The evaluator runs outside your container.
