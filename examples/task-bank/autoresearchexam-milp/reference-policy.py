"""Branch on the most fractional candidate."""

import numpy as np


class Policy:
    def select(self, obs):
        frac = obs["cand_feats"][:, 4]
        return int(np.argmax(np.minimum(frac, 1.0 - frac)))
