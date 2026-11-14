from .io import array, text

def memberships(data, unit_ids):
    """Validate a top-level domain-name -> unit_id array mapping."""
    if not isinstance(data, dict):
        raise ValueError('domains must be a JSON object mapping names to unit_id arrays')
    result = {}
    for name, units in data.items():
        name = text(name)
        members = set()
        for uid in array(units):
            uid = text(uid)
            if uid in members:
                raise ValueError('duplicate unit in domain: ' + uid)
            if uid not in unit_ids:
                raise ValueError('domain references unknown unit: ' + uid)
            members.add(uid)
        result[name] = members
    return result
