Each point in `/app/data/train.csv` has a number `x` between 0 and 1 and a
`label`, 0 or 1. A point is predicted 1 when `x` is greater than a threshold.

Write `/workspace/candidate/model.json` holding one JSON object, `{"threshold": NUMBER}`,
so that the predictions are right on as many unseen points as possible.

The score is the accuracy of those predictions on points you cannot read. A
`model.json` that is missing, is not that object, or has a threshold outside
0 to 1 scores 0.
