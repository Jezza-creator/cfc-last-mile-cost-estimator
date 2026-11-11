from __future__ import annotations

from dataclasses import dataclass
import statistics


@dataclass(frozen=True)
class EnvelopeEstimate:
    estimated_length_cm: float | None
    estimated_width_cm: float | None
    estimated_height_cm: float | None
    estimated_cube_cm3: float
    shape_source: str


def normalize_dimension_triplet(
    length_cm: float | None,
    width_cm: float | None,
    height_cm: float | None,
) -> tuple[float, float, float] | None:
    dims = [float(value) for value in (length_cm, width_cm, height_cm) if value is not None and float(value) > 0]
    if len(dims) < 3:
        return None
    dims.sort(reverse=True)
    return dims[0], dims[1], dims[2]


def estimate_rectangular_envelope(
    dimension_triplets: list[tuple[float, float, float]],
    packed_cube_cm3: float,
) -> EnvelopeEstimate | None:
    if not dimension_triplets or packed_cube_cm3 <= 0:
        return None

    long_edges = [dims[0] for dims in dimension_triplets]
    mid_edges = [dims[1] for dims in dimension_triplets]
    short_edges = [dims[2] for dims in dimension_triplets]

    estimated_length_cm = max(long_edges)
    estimated_width_cm = statistics.median(mid_edges)

    if estimated_length_cm <= 0 or estimated_width_cm <= 0:
        return None

    min_height_from_volume = packed_cube_cm3 / (estimated_length_cm * estimated_width_cm)
    estimated_height_cm = max(max(short_edges), min_height_from_volume)
    estimated_cube_cm3 = estimated_length_cm * estimated_width_cm * estimated_height_cm

    if estimated_cube_cm3 < packed_cube_cm3:
        estimated_cube_cm3 = packed_cube_cm3
        estimated_height_cm = estimated_cube_cm3 / (estimated_length_cm * estimated_width_cm)

    return EnvelopeEstimate(
        estimated_length_cm=round(estimated_length_cm, 1),
        estimated_width_cm=round(estimated_width_cm, 1),
        estimated_height_cm=round(estimated_height_cm, 1),
        estimated_cube_cm3=estimated_cube_cm3,
        shape_source="RECTANGULAR_ENVELOPE",
    )


def estimate_stacked_rectangular_envelope(
    dimension_triplets: list[tuple[float, float, float]],
    packed_cube_cm3: float,
    *,
    max_reflow_length_cm: float = 60.0,
    max_reflow_width_cm: float = 40.0,
    reflow_height_trigger_cm: float = 60.0,
) -> EnvelopeEstimate | None:
    """Create a transparent, conservative parcel envelope before box matching.

    Items are normalised to long/mid/short orientation. The base uses the
    longest item and the median middle edge; short edges are stacked. This is
    deliberately a disclosed baseline, rather than pretending to solve an
    unknown box suite or exact 3-D packing layout.
    """
    if not dimension_triplets or packed_cube_cm3 <= 0:
        return None

    long_edges = [dims[0] for dims in dimension_triplets]
    mid_edges = [dims[1] for dims in dimension_triplets]
    short_edges = [dims[2] for dims in dimension_triplets]
    estimated_length_cm = max(long_edges)
    estimated_width_cm = statistics.median(mid_edges)
    if estimated_length_cm <= 0 or estimated_width_cm <= 0:
        return None

    # Retain physical volume as a hard lower bound for heterogeneous products.
    height_from_volume = packed_cube_cm3 / (estimated_length_cm * estimated_width_cm)
    estimated_height_cm = max(sum(short_edges), height_from_volume)
    estimated_cube_cm3 = estimated_length_cm * estimated_width_cm * estimated_height_cm

    # Very tall stacks are re-flowed across a practical carton footprint. This
    # preserves the conservative cube while avoiding implausible vertical tubes.
    can_fit_reflow_footprint = (
        max(long_edges) <= max_reflow_length_cm
        and max(mid_edges) <= max_reflow_width_cm
    )
    if estimated_height_cm > reflow_height_trigger_cm and can_fit_reflow_footprint:
        estimated_length_cm = max_reflow_length_cm
        estimated_width_cm = max_reflow_width_cm
        estimated_height_cm = max(
            max(short_edges),
            estimated_cube_cm3 / (estimated_length_cm * estimated_width_cm),
        )
        shape_source = "STACKED_RECTANGULAR_ENVELOPE_REFLOWED_60X40"
    else:
        shape_source = "STACKED_RECTANGULAR_ENVELOPE"

    return EnvelopeEstimate(
        estimated_length_cm=round(estimated_length_cm, 1),
        estimated_width_cm=round(estimated_width_cm, 1),
        estimated_height_cm=round(estimated_height_cm, 1),
        estimated_cube_cm3=estimated_cube_cm3,
        shape_source=shape_source,
    )
