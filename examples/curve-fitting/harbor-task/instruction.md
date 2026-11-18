Improve `/workspace/candidate/coefficients.json` so that its polynomial fits the thirteen
`(x, y)` observations in `/app/data/observations.json` more closely.

The file holds `{"coefficients": [...]}` in ascending powers: `[c0, c1, c2]`
means `y = c0 + c1*x + c2*x*x`. The starting file is the constant zero, `[0.0]`.

`rmse` is the root mean squared error on those thirteen observations. Fitting
them is the task; there is no hidden test set.

Read the observations and the baseline result first, then compare simple
polynomial fits. Python's standard library is enough to calculate one in `/tmp`.
