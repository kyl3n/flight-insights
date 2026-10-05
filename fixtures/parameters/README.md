These tiny files are **synthetic format and arithmetic fixtures**. They are not
aircraft data, accepted baselines, or proposed tuning values. No fixture is
automatically imported. Real Q22 original-before-Brian and Brian files are still
required before their protected baseline labels can be established.

`synthetic-before.param` uses Mission Planner comma records; `synthetic-after.parm`
uses whitespace records. `synthetic-qgc.params` uses QGroundControl's five fields:
MAVLink system ID, component ID, parameter name, value, and MAV_PARAM_TYPE.

The performance fixtures use the documented explicit-unit adapter. Constant data
has 100 W for four seconds and a constant 3 degree roll tracking error; the second
fixture has 80 W and 2 degrees. Those deliberately simple differences validate
arithmetic, and do not validate device integrations or parameter causality.
