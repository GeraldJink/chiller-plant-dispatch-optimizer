"""Example custom allocation: reserve minimum load, then fill preferred units."""
from load_allocation import AllocationInfeasible


def create_priority_allocator(options):
    if set(options) != {"priority_ids"}:
        raise ValueError("Expected priority_ids")
    ids = options["priority_ids"]
    if not isinstance(ids, list) or any(not isinstance(name, str) or not name for name in ids) or len(ids) != len(set(ids)):
        raise ValueError("priority_ids must contain unique nonempty device IDs")
    priority = {name: i for i, name in enumerate(ids)}

    def allocate(inputs):
        group = sorted(inputs.chillers, key=lambda d: (priority.get(d.id, len(priority)), d.id))
        result = {d.id: d.capacity_kw * d.min_plr for d in group}
        remaining = inputs.load_kw - sum(result.values())
        if remaining < -1e-6:
            raise AllocationInfeasible("Demand is below combined minimum cooling output")
        for d in group:
            extra = min(max(0.0, remaining), d.capacity_kw * d.max_plr - result[d.id])
            result[d.id] += extra
            remaining -= extra
        if remaining > 1e-6:
            raise AllocationInfeasible("Demand exceeds combined capacity")
        return result

    return allocate
