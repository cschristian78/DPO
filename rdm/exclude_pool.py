"""Excluding-curve workers. Each process keeps the portfolio ELT and one risk."""

from rdm.analysis import excluding_pml

_STATE = {}


def init_worker(event_ids, mean, std, exposure, rate, points, ceilings):
    """Store the portfolio event-loss table and its return-period losses."""
    _STATE["event_ids"] = event_ids
    _STATE["mean"] = mean
    _STATE["std"] = std
    _STATE["exposure"] = exposure
    _STATE["rate"] = rate
    _STATE["points"] = points
    _STATE["ceilings"] = ceilings


def exclude_risk(entity_id, losses):
    """Return one risk's excluding curve, annual loss, and standard deviation."""
    values, aal, stddev = excluding_pml(
        _STATE["event_ids"], _STATE["mean"], _STATE["std"], _STATE["exposure"],
        _STATE["rate"], losses, _STATE["points"], _STATE["ceilings"])
    del losses
    return (
        str(entity_id),
        [float(value) for value in values],
        float(aal),
        float(stddev))
