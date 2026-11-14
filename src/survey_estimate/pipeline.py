from .io import load
from .estimate import estimate

def run(path):
    return estimate(load(path))
