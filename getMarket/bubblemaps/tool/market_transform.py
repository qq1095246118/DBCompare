import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import (
    Clamped,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    ROUND_HALF_EVEN,
    Subnormal,
    Underflow,
)
from math import isfinite

from getMarket.bubblemaps.tool.market_identity import (
    TargetToken,
    canonicalize_address,
    make_target,
    token_ref_matches,
)


@dataclass(frozen=True)
class RankedHolder:
    address: str
    source_rank: int
    amount: str
    share: str
    share_percent: str
    is_supernode: bool
    metadata: dict


@dataclass(frozen=True)
class SubgraphEdge:
    from_address: str
    to_address: str
    total_transfers: int
    raw: dict


@dataclass(frozen=True)
class Cluster:
    cluster_rank: int
    amount: str
    share: str
    share_percent: str
    members: tuple[RankedHolder, ...]


@dataclass(frozen=True)
class SnapshotModel:
    target: TargetToken
    holders: tuple[RankedHolder, ...]
    edges: tuple[SubgraphEdge, ...]
    clusters: tuple[Cluster, ...]
    fingerprint: str
    captured_at: str


_METADATA_FIELDS = (
    "label",
    "entity_id",
    "is_contract",
    "is_cex",
    "is_dex",
    "degree",
    "inward_relations",
    "outward_relations",
    "first_activity_date",
)
_EDGE_COMPLETENESS_FIELDS = (
    "first_date",
    "last_date",
    "total_value",
)
_MAX_DECIMAL_DIGITS = 10_000
_MAX_ABS_ADJUSTED_EXPONENT = 10_000
_MAX_DECIMAL_TEXT_LENGTH = 20_000
_MAX_ABS_LEXICAL_EXPONENT = 20_000
_DECIMAL_TEXT_PATTERN = re.compile(
    r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))"
    r"(?:[eE](?P<exponent_sign>[+-]?)(?P<exponent>[0-9]+))?",
    re.ASCII,
)
_NONFINITE_DECIMAL_TEXT_PATTERN = re.compile(
    r"[+-]?(?:inf(?:inity)?|s?nan[0-9]*)",
    re.ASCII | re.IGNORECASE,
)
_EVM_ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}", re.ASCII)
_HUNDRED = Decimal(100)


def _decimal_context() -> Context:
    return Context(
        prec=30_010,
        rounding=ROUND_HALF_EVEN,
        Emin=-20_010,
        Emax=20_010,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[
            Clamped,
            DivisionByZero,
            FloatOperation,
            Inexact,
            InvalidOperation,
            Overflow,
            Rounded,
            Subnormal,
            Underflow,
        ],
    )


def _decimal_value(value: object, description: str) -> tuple[Decimal, str]:
    if type(value) not in (str, int):
        raise ValueError(
            f"{description} must be supplied as a decimal string or native integer"
        )

    if type(value) is int:
        try:
            number = Decimal(value, context=_decimal_context())
        except (DecimalException, TypeError, ValueError):
            raise ValueError(
                f"{description} must be a decimal-compatible value"
            ) from None
    else:
        text = value
        if len(text) > _MAX_DECIMAL_TEXT_LENGTH:
            raise ValueError(
                f"{description} source text exceeds limit of "
                f"{_MAX_DECIMAL_TEXT_LENGTH} characters"
            )
        if _NONFINITE_DECIMAL_TEXT_PATTERN.fullmatch(text):
            raise ValueError(f"{description} must be a finite decimal")

        match = _DECIMAL_TEXT_PATTERN.fullmatch(text)
        if match is None:
            raise ValueError(f"{description} has invalid decimal syntax")

        exponent_text = match.group("exponent")
        if exponent_text is not None:
            significant_exponent = exponent_text.lstrip("0") or "0"
            exponent_limit_text = str(_MAX_ABS_LEXICAL_EXPONENT)
            if (
                len(significant_exponent) > len(exponent_limit_text)
                or (
                    len(significant_exponent) == len(exponent_limit_text)
                    and significant_exponent > exponent_limit_text
                )
            ):
                raise ValueError(
                    f"{description} exponent text is outside supported lexical "
                    f"range [-{_MAX_ABS_LEXICAL_EXPONENT}, "
                    f"{_MAX_ABS_LEXICAL_EXPONENT}]"
                )

        try:
            number = Decimal(text, context=_decimal_context())
        except (DecimalException, TypeError, ValueError):
            raise ValueError(
                f"{description} must be a decimal-compatible value"
            ) from None

    if not number.is_finite():
        raise ValueError(f"{description} must be a finite decimal")
    if number < 0:
        raise ValueError(f"{description} must be nonnegative")
    if len(number.as_tuple().digits) > _MAX_DECIMAL_DIGITS:
        raise ValueError(
            f"{description} exceeds supported precision of "
            f"{_MAX_DECIMAL_DIGITS} significant digits"
        )
    if abs(number.adjusted()) > _MAX_ABS_ADJUSTED_EXPONENT:
        raise ValueError(
            f"{description} exceeds supported exponent range "
            f"[-{_MAX_ABS_ADJUSTED_EXPONENT}, "
            f"{_MAX_ABS_ADJUSTED_EXPONENT}]"
        )

    if number.is_zero():
        number = number.copy_abs()
    return number, format(number, "f")


def _percent_text(share: Decimal) -> str:
    return format(_decimal_context().multiply(share, _HUNDRED), "f")


def _metadata_from_details(details: Mapping, description: str) -> dict:
    metadata: dict = {}
    for field in _METADATA_FIELDS:
        if field not in details:
            continue
        value = details[field]
        if value is not None and type(value) not in (str, int, float, bool):
            raise ValueError(
                f"{description} metadata field {field!r} must be a JSON scalar"
            )
        if type(value) is float and not isfinite(value):
            raise ValueError(
                f"{description} metadata field {field!r} must be a JSON scalar"
            )
        metadata[field] = value
    return metadata


def parse_ranked_holders(
    payload: object,
    *,
    target: TargetToken,
) -> tuple[RankedHolder, ...]:
    if not isinstance(payload, list):
        raise ValueError("holder payload must be a top-level list")
    if not payload:
        raise ValueError("holder payload must contain at least one ranked holder")

    holders: list[RankedHolder] = []
    seen_addresses: set[str] = set()
    seen_ranks: set[int] = set()

    for index, row in enumerate(payload):
        description = f"holder row {index}"
        if not isinstance(row, Mapping):
            raise ValueError(f"{description} must be an object")

        holder_data = row.get("holder_data")
        if not isinstance(holder_data, Mapping):
            raise ValueError(f"{description} field 'holder_data' must be an object")
        if "rank" not in holder_data:
            raise ValueError(f"{description} is missing holder rank")

        rank = holder_data["rank"]
        if rank is None:
            continue
        if type(rank) is not int or rank <= 0:
            raise ValueError(
                f"{description} rank must be a positive native integer"
            )

        try:
            address = canonicalize_address(target.chain, row.get("address"))
        except ValueError as error:
            raise ValueError(f"{description} has invalid address: {error}") from None

        if address in seen_addresses:
            raise ValueError(f"duplicate holder address: {address}")
        if rank in seen_ranks:
            raise ValueError(f"duplicate holder rank: {rank}")

        amount, amount_text = _decimal_value(
            holder_data.get("amount"),
            f"{description} amount",
        )
        share, share_text = _decimal_value(
            holder_data.get("share"),
            f"{description} share",
        )

        details = row.get("address_details")
        if not isinstance(details, Mapping):
            raise ValueError(
                f"{description} field 'address_details' must be an object"
            )
        if "is_supernode" not in details or type(details["is_supernode"]) is not bool:
            raise ValueError(
                f"{description} field 'is_supernode' must be a native bool"
            )

        holders.append(
            RankedHolder(
                address=address,
                source_rank=rank,
                amount=amount_text,
                share=share_text,
                share_percent=_percent_text(share),
                is_supernode=details["is_supernode"],
                metadata=_metadata_from_details(details, description),
            )
        )
        seen_addresses.add(address)
        seen_ranks.add(rank)

    if not holders:
        raise ValueError("holder payload contains no ranked holders")

    return tuple(sorted(holders, key=lambda holder: holder.source_rank))


def _canonical_json(value: object, description: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError(f"{description} must contain valid JSON values") from None


def _copied_edge_core(data: Mapping, total_transfers: int) -> dict:
    core = {"total_transfers": total_transfers}
    for field in _EDGE_COMPLETENESS_FIELDS:
        if field in data:
            core[field] = copy.deepcopy(data[field])
    return core


def filter_subgraph_edges(
    payload: object,
    *,
    target: TargetToken,
    holders: Mapping[str, RankedHolder],
) -> tuple[SubgraphEdge, ...]:
    if not isinstance(payload, list):
        raise ValueError("subgraph payload must be a top-level list")

    selected: dict[
        tuple[str, str, str, str, str],
        tuple[SubgraphEdge, str, str],
    ] = {}

    for index, row in enumerate(payload):
        if not isinstance(row, Mapping):
            continue
        if row.get("rel_type") != "GROUPED_TRANSFER":
            continue

        data = row.get("data")
        if not isinstance(data, Mapping):
            continue
        if not token_ref_matches(data.get("token_ref"), target):
            continue

        try:
            from_address = canonicalize_address(
                target.chain,
                row.get("from_address"),
            )
            to_address = canonicalize_address(
                target.chain,
                row.get("to_address"),
            )
        except ValueError:
            continue

        if from_address not in holders or to_address not in holders:
            continue

        total_transfers = data.get("total_transfers")
        if type(total_transfers) is not int or total_transfers <= 0:
            raise ValueError(
                f"subgraph row {index} total_transfers must be a positive "
                "native integer"
            )

        try:
            raw = copy.deepcopy(dict(row))
            core = _copied_edge_core(data, total_transfers)
        except Exception:
            raise ValueError(
                f"subgraph row {index} could not be copied safely"
            ) from None

        core_signature = _canonical_json(core, f"subgraph row {index} core data")
        raw_signature = _canonical_json(raw, f"subgraph row {index}")
        key = (
            from_address,
            to_address,
            "GROUPED_TRANSFER",
            target.chain,
            target.token_address,
        )
        edge = SubgraphEdge(
            from_address=from_address,
            to_address=to_address,
            total_transfers=total_transfers,
            raw=raw,
        )

        previous = selected.get(key)
        if previous is None:
            selected[key] = (edge, core_signature, raw_signature)
            continue

        previous_edge, previous_core, previous_raw = previous
        if previous_core != core_signature:
            raise ValueError(
                "conflicting duplicate edge for "
                f"{from_address} -> {to_address}"
            )
        if raw_signature < previous_raw:
            selected[key] = (edge, core_signature, raw_signature)
        else:
            selected[key] = (
                previous_edge,
                previous_core,
                previous_raw,
            )

    ordered = sorted(
        selected.items(),
        key=lambda item: item[0],
    )
    return tuple(value[0] for _, value in ordered)


def _holder_decimal(holder: RankedHolder, field: str) -> Decimal:
    try:
        return Decimal(getattr(holder, field))
    except (DecimalException, TypeError, ValueError):
        raise ValueError(f"holder {holder.address} has invalid {field}") from None


def _member_sort_key(holder: RankedHolder) -> tuple[Decimal, Decimal, int, str]:
    return (
        _holder_decimal(holder, "amount").copy_negate(),
        _holder_decimal(holder, "share").copy_negate(),
        holder.source_rank,
        holder.address,
    )


def _exact_sum(holders: Sequence[RankedHolder], field: str) -> Decimal:
    context = _decimal_context()
    total = Decimal(0)
    try:
        for holder in holders:
            total = context.add(total, _holder_decimal(holder, field))
    except DecimalException:
        raise ValueError(f"cluster {field} exceeds supported decimal limits") from None
    return total


def reconstruct_clusters(
    holders: Sequence[RankedHolder],
    edges: Sequence[SubgraphEdge],
) -> tuple[Cluster, ...]:
    holder_by_address = {holder.address: holder for holder in holders}
    adjacency = {address: set() for address in holder_by_address}

    for edge in edges:
        from_address = edge.from_address
        to_address = edge.to_address
        if (
            from_address == to_address
            or from_address not in adjacency
            or to_address not in adjacency
        ):
            continue
        adjacency[from_address].add(to_address)
        adjacency[to_address].add(from_address)

    components: list[
        tuple[Decimal, Decimal, tuple[str, ...], tuple[RankedHolder, ...]]
    ] = []
    seen: set[str] = set()

    for root in sorted(adjacency):
        if root in seen:
            continue
        stack = [root]
        seen.add(root)
        component_addresses: list[str] = []
        while stack:
            address = stack.pop()
            component_addresses.append(address)
            for neighbor in sorted(adjacency[address], reverse=True):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)

        if len(component_addresses) < 2:
            continue

        members = tuple(
            sorted(
                (holder_by_address[address] for address in component_addresses),
                key=_member_sort_key,
            )
        )
        amount = _exact_sum(members, "amount")
        share = _exact_sum(members, "share")
        components.append(
            (
                amount,
                share,
                tuple(sorted(component_addresses)),
                members,
            )
        )

    components.sort(
        key=lambda item: (
            item[0].copy_negate(),
            item[1].copy_negate(),
            item[2],
        )
    )

    return tuple(
        Cluster(
            cluster_rank=cluster_rank,
            amount=format(amount, "f"),
            share=format(share, "f"),
            share_percent=_percent_text(share),
            members=members,
        )
        for cluster_rank, (amount, share, _addresses, members) in enumerate(
            components,
            start=1,
        )
    )


def _fingerprint_address(address: str) -> str:
    if _EVM_ADDRESS_PATTERN.fullmatch(address):
        return address.lower()
    return address


def _fingerprint_token_ref(raw: Mapping) -> dict:
    data = raw.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("selected edge raw data must be an object")
    token_ref = data.get("token_ref")
    if not isinstance(token_ref, Mapping):
        raise ValueError("selected edge token_ref must be an object")
    try:
        target = make_target(token_ref.get("chain"), token_ref.get("address"))
    except ValueError:
        raise ValueError("selected edge token_ref is malformed") from None
    return {"chain": target.chain, "address": target.token_address}


def _fingerprint_edge(edge: SubgraphEdge) -> dict:
    if not isinstance(edge.raw, Mapping):
        raise ValueError("selected edge raw value must be an object")
    data = edge.raw.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("selected edge raw data must be an object")

    formal = {
        "from_address": _fingerprint_address(edge.from_address),
        "to_address": _fingerprint_address(edge.to_address),
        "rel_type": edge.raw.get("rel_type"),
        "total_transfers": edge.total_transfers,
        "token_ref": _fingerprint_token_ref(edge.raw),
    }
    for field in _EDGE_COMPLETENESS_FIELDS:
        if field in data:
            formal[field] = copy.deepcopy(data[field])
    return formal


def token_snapshot_fingerprint(
    holders: Sequence[RankedHolder],
    edges: Sequence[SubgraphEdge],
) -> str:
    formal_holders = [
        {
            "address": _fingerprint_address(holder.address),
            "source_rank": holder.source_rank,
            "amount": holder.amount,
            "share": holder.share,
            "share_percent": holder.share_percent,
            "is_supernode": holder.is_supernode,
            "metadata": copy.deepcopy(holder.metadata),
        }
        for holder in holders
    ]
    formal_edges = [_fingerprint_edge(edge) for edge in edges]

    formal_holders.sort(
        key=lambda holder: _canonical_json(holder, "formal holder")
    )
    formal_edges.sort(key=lambda edge: _canonical_json(edge, "formal edge"))
    canonical = _canonical_json(
        {"holders": formal_holders, "edges": formal_edges},
        "token snapshot",
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
