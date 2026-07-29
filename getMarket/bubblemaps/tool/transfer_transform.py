import copy
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from math import isfinite

from common.artifacts import safe_path_component
from getMarket.bubblemaps.tool.market_identity import (
    TargetToken,
    canonicalize_address,
    token_ref_matches,
)
from getMarket.bubblemaps.tool.market_transform import (
    Cluster,
    RankedHolder,
    SubgraphEdge,
)


@dataclass(frozen=True)
class TransferResult:
    token_document: dict
    member_documents: dict[str, dict]
    unique_transfer_count: int
    transfer_view_count: int
    count_drifts: tuple[dict, ...]


class AmbiguousTransferIdentityError(ValueError):
    pass


class TransferCompletenessError(ValueError):
    pass


@dataclass(frozen=True)
class _MemberContext:
    holder: RankedHolder
    cluster_index: int
    cluster_rank: int


@dataclass(frozen=True)
class _FilteredTransfer:
    raw_record: dict
    from_address: str
    to_address: str
    date: int
    value_text: str


_FallbackIdentity = tuple[str, str, str, str, str, int, str]

_DECIMAL_TEXT_PATTERN = re.compile(
    r"(?P<sign>[+-]?)"
    r"(?:(?P<integer>[0-9]+)(?:\.(?P<fraction>[0-9]*))?"
    r"|\.(?P<fraction_only>[0-9]+))"
    r"(?:[eE](?P<exponent_sign>[+-]?)(?P<exponent>[0-9]+))?",
    re.ASCII,
)


def _validate_json_native(value: object, description: str) -> None:
    active_containers: set[int] = set()

    def visit(current: object) -> None:
        current_type = type(current)
        if current_type is dict:
            container_id = id(current)
            if container_id in active_containers:
                raise ValueError(
                    f"{description} must contain acyclic JSON-native data"
                )
            active_containers.add(container_id)
            try:
                for key, child in current.items():
                    if type(key) is not str:
                        raise ValueError(
                            f"{description} JSON object keys must be native strings"
                        )
                    visit(child)
            finally:
                active_containers.remove(container_id)
            return

        if current_type is list:
            container_id = id(current)
            if container_id in active_containers:
                raise ValueError(
                    f"{description} must contain acyclic JSON-native data"
                )
            active_containers.add(container_id)
            try:
                for child in current:
                    visit(child)
            finally:
                active_containers.remove(container_id)
            return

        if current is None or current_type in (str, bool, int):
            return
        if current_type is float:
            if not isfinite(current):
                raise ValueError(
                    f"{description} JSON float values must be finite"
                )
            return
        raise ValueError(f"{description} must contain only JSON-native values")

    try:
        visit(value)
    except RecursionError:
        raise ValueError(
            f"{description} exceeds JSON nesting supported by the runtime"
        ) from None


def _native_int_text(value: int) -> str:
    sign, digits, exponent = Decimal(value).as_tuple()
    digit_text = "".join(chr(ord("0") + digit) for digit in digits)
    if exponent > 0:
        digit_text += "0" * exponent
    elif exponent < 0:
        decimal_point = len(digit_text) + exponent
        if decimal_point <= 0:
            digit_text = "0." + "0" * (-decimal_point) + digit_text
        else:
            digit_text = (
                digit_text[:decimal_point] + "." + digit_text[decimal_point:]
            )
    return ("-" if sign else "") + digit_text


def _dump_json_native(value: object) -> str:
    value_type = type(value)
    if value_type is dict:
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False)
            + ":"
            + _dump_json_native(value[key])
            for key in sorted(value)
        ) + "}"
    if value_type is list:
        return "[" + ",".join(_dump_json_native(child) for child in value) + "]"
    if value is None:
        return "null"
    if value_type is str:
        return json.dumps(value, ensure_ascii=False)
    if value_type is bool:
        return "true" if value else "false"
    if value_type is int:
        return _native_int_text(value)
    if value_type is float:
        return json.dumps(value, allow_nan=False)
    raise AssertionError("JSON-native validation must run before serialization")


def _canonical_json(value: object, description: str) -> str:
    _validate_json_native(value, description)
    try:
        return _dump_json_native(value)
    except (TypeError, ValueError, RecursionError, DecimalException):
        raise ValueError(
            f"{description} must contain valid JSON values"
        ) from None


def _compare_decimal_magnitudes(left: str, right: str) -> int:
    if len(left) != len(right):
        return 1 if len(left) > len(right) else -1
    if left == right:
        return 0
    return 1 if left > right else -1


def _add_decimal_magnitudes(left: str, right: str) -> str:
    left_index = len(left) - 1
    right_index = len(right) - 1
    carry = 0
    reversed_digits: list[str] = []
    while left_index >= 0 or right_index >= 0 or carry:
        total = carry
        if left_index >= 0:
            total += ord(left[left_index]) - ord("0")
            left_index -= 1
        if right_index >= 0:
            total += ord(right[right_index]) - ord("0")
            right_index -= 1
        reversed_digits.append(chr(ord("0") + total % 10))
        carry = total // 10
    return "".join(reversed(reversed_digits))


def _subtract_decimal_magnitudes(larger: str, smaller: str) -> str:
    larger_index = len(larger) - 1
    smaller_index = len(smaller) - 1
    borrow = 0
    reversed_digits: list[str] = []
    while larger_index >= 0:
        difference = ord(larger[larger_index]) - ord("0") - borrow
        if smaller_index >= 0:
            difference -= ord(smaller[smaller_index]) - ord("0")
            smaller_index -= 1
        if difference < 0:
            difference += 10
            borrow = 1
        else:
            borrow = 0
        reversed_digits.append(chr(ord("0") + difference))
        larger_index -= 1
    return "".join(reversed(reversed_digits)).lstrip("0") or "0"


def _adjust_lexical_exponent(
    exponent_sign: str,
    exponent_digits: str,
    offset: int,
) -> str:
    magnitude = exponent_digits.lstrip("0") or "0"
    base_sign = -1 if exponent_sign == "-" and magnitude != "0" else 0
    if base_sign == 0 and magnitude != "0":
        base_sign = 1

    offset_sign = -1 if offset < 0 else (1 if offset > 0 else 0)
    offset_magnitude = str(abs(offset))
    if base_sign == 0:
        result_sign = offset_sign
        result_magnitude = offset_magnitude
    elif offset_sign == 0:
        result_sign = base_sign
        result_magnitude = magnitude
    elif base_sign == offset_sign:
        result_sign = base_sign
        result_magnitude = _add_decimal_magnitudes(
            magnitude,
            offset_magnitude,
        )
    else:
        comparison = _compare_decimal_magnitudes(magnitude, offset_magnitude)
        if comparison == 0:
            return "0"
        if comparison > 0:
            result_sign = base_sign
            result_magnitude = _subtract_decimal_magnitudes(
                magnitude,
                offset_magnitude,
            )
        else:
            result_sign = offset_sign
            result_magnitude = _subtract_decimal_magnitudes(
                offset_magnitude,
                magnitude,
            )
    return ("-" if result_sign < 0 else "") + result_magnitude


def _canonical_string_decimal_value(value: str, description: str) -> str:
    match = _DECIMAL_TEXT_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"{description} has invalid decimal syntax")

    integer = match.group("integer") or ""
    fraction = match.group("fraction")
    if fraction is None:
        fraction = match.group("fraction_only") or ""
    coefficient_source = integer + fraction
    significant = coefficient_source.lstrip("0")
    if not significant:
        return "0"
    if match.group("sign") == "-":
        raise ValueError(f"{description} must be nonnegative")

    coefficient = significant.rstrip("0")
    stripped_trailing_zeroes = len(significant) - len(coefficient)
    exponent = _adjust_lexical_exponent(
        match.group("exponent_sign") or "",
        match.group("exponent") or "0",
        stripped_trailing_zeroes - len(fraction),
    )
    return f"{coefficient}e{exponent}"


def _canonical_decimal_value(value: object, description: str) -> str:
    if type(value) is str:
        return _canonical_string_decimal_value(value, description)
    if type(value) is not int:
        raise ValueError(
            f"{description} must be an exact decimal string or native integer"
        )

    try:
        number = Decimal(value)
    except (DecimalException, TypeError, ValueError):
        raise ValueError(
            f"{description} must be an exact decimal string or native integer"
        ) from None
    if number < 0:
        raise ValueError(f"{description} must be nonnegative")
    if number.is_zero():
        return "0"

    _sign, coefficient, exponent = number.as_tuple()
    digits = list(coefficient)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    digit_text = "".join(chr(ord("0") + digit) for digit in digits)
    return f"{digit_text}e{exponent}"


def _build_member_index(
    clusters: Sequence[Cluster],
    *,
    target: TargetToken,
) -> dict[str, _MemberContext]:
    member_index: dict[str, _MemberContext] = {}
    seen_cluster_ranks: set[int] = set()
    seen_source_ranks: set[int] = set()
    try:
        cluster_values = tuple(clusters)
    except (TypeError, RecursionError):
        raise ValueError("clusters must be a sequence") from None

    for cluster_index, cluster in enumerate(cluster_values):
        try:
            cluster_rank = cluster.cluster_rank
        except AttributeError:
            raise ValueError(f"cluster {cluster_index} is malformed") from None
        if type(cluster_rank) is not int or cluster_rank <= 0:
            raise ValueError(
                f"cluster {cluster_index} cluster_rank must be a positive "
                "native integer"
            )
        if cluster_rank in seen_cluster_ranks:
            raise ValueError(f"duplicate cluster_rank: {cluster_rank}")
        expected_cluster_rank = cluster_index + 1
        if cluster_rank != expected_cluster_rank:
            raise ValueError(
                f"cluster {cluster_index} cluster_rank must be "
                f"{expected_cluster_rank} in supplied order"
            )
        seen_cluster_ranks.add(cluster_rank)

        try:
            members = tuple(cluster.members)
        except (AttributeError, TypeError, RecursionError):
            raise ValueError(f"cluster {cluster_index} is malformed") from None

        for member_index_in_cluster, member in enumerate(members):
            try:
                source_rank = member.source_rank
                source_address = member.address
            except AttributeError:
                raise ValueError(
                    f"cluster {cluster_index} member {member_index_in_cluster} "
                    "is malformed"
                ) from None
            if type(source_rank) is not int or source_rank <= 0:
                raise ValueError(
                    f"cluster {cluster_index} member {member_index_in_cluster} "
                    "source_rank must be a positive native integer"
                )
            if source_rank in seen_source_ranks:
                raise ValueError(f"duplicate source_rank: {source_rank}")
            seen_source_ranks.add(source_rank)

            try:
                canonical_address = canonicalize_address(
                    target.chain,
                    source_address,
                )
            except ValueError:
                raise ValueError(
                    f"cluster {cluster_index} member {member_index_in_cluster} "
                    "has an invalid address"
                ) from None
            if type(source_address) is not str or source_address != canonical_address:
                raise ValueError(
                    f"cluster member address must already be canonical: "
                    f"{source_address!r}"
                )
            if canonical_address in member_index:
                raise ValueError(
                    f"duplicate Cluster member address: {canonical_address}"
                )
            member_index[canonical_address] = _MemberContext(
                holder=member,
                cluster_index=cluster_index,
                cluster_rank=cluster_rank,
            )

    return member_index


def _validated_payloads(
    payloads_by_member: Mapping[str, object],
    member_index: Mapping[str, _MemberContext],
    unavailable_members: frozenset[str],
) -> dict[str, list]:
    if not isinstance(payloads_by_member, Mapping):
        raise ValueError("payloads_by_member must be a mapping")

    ordinary_addresses = tuple(
        address
        for address, context in member_index.items()
        if not context.holder.is_supernode and address not in unavailable_members
    )
    expected_keys = set(ordinary_addresses)
    try:
        source_keys = tuple(payloads_by_member)
        actual_keys = set(source_keys)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("payload member keys could not be inspected") from None
    if (
        any(type(key) is not str for key in source_keys)
        or actual_keys != expected_keys
        or len(source_keys) != len(expected_keys)
    ):
        raise ValueError(
            "payload member keys must exactly equal the complete canonical "
            "ordinary Cluster member set"
        )

    payloads: dict[str, list] = {}
    for address in ordinary_addresses:
        try:
            payload = payloads_by_member[address]
        except Exception:
            raise ValueError(
                f"payload for ordinary member {address} could not be read"
            ) from None
        if not isinstance(payload, list):
            raise ValueError(
                f"payload for ordinary member {address} must be a top-level list"
            )
        payloads[address] = payload
    return payloads


def _mapping_field(
    value: Mapping,
    field: str,
    description: str,
) -> object:
    try:
        return value.get(field)
    except Exception:
        raise ValueError(
            f"{description} field {field!r} could not be read"
        ) from None


def _filter_member_payload(
    payload: object,
    *,
    capture_member: str,
    target: TargetToken,
) -> tuple[_FilteredTransfer, ...]:
    if not isinstance(payload, list):
        raise ValueError(
            f"payload for ordinary member {capture_member} must be a top-level list"
        )

    selected: list[_FilteredTransfer] = []
    selected_signatures: dict[_FallbackIdentity, str] = {}
    for row_index, row in enumerate(payload):
        description = f"transfer response for {capture_member} row {row_index}"
        if not isinstance(row, Mapping):
            raise ValueError(f"{description} must be an object")

        if _mapping_field(row, "rel_type", description) != "TRANSFER":
            continue
        data = _mapping_field(row, "data", description)
        if not isinstance(data, Mapping):
            raise ValueError(f"{description} data must be an object")

        token_ref = _mapping_field(data, "token_ref", f"{description} data")
        if not token_ref_matches(token_ref, target):
            continue

        source_from = _mapping_field(row, "from_address", description)
        source_to = _mapping_field(row, "to_address", description)
        try:
            from_address = canonicalize_address(target.chain, source_from)
            to_address = canonicalize_address(target.chain, source_to)
        except ValueError:
            continue

        if capture_member not in (from_address, to_address):
            raise ValueError(
                f"{description} does not contain requested capture member "
                f"{capture_member}"
            )

        tx_hash = _mapping_field(data, "tx_hash", f"{description} data")
        if type(tx_hash) is not str or not tx_hash:
            raise ValueError(
                f"{description} data tx_hash must be a non-empty native string"
            )
        date = _mapping_field(data, "date", f"{description} data")
        if type(date) is not int or date < 0:
            raise ValueError(
                f"{description} data date must be a nonnegative native integer"
            )
        value = _mapping_field(data, "value", f"{description} data")
        value_text = _canonical_decimal_value(
            value,
            f"{description} data value",
        )

        _validate_json_native(row, description)
        try:
            raw_record = copy.deepcopy(row)
        except Exception:
            raise ValueError(f"{description} could not be copied safely") from None
        filtered = _FilteredTransfer(
            raw_record=raw_record,
            from_address=from_address,
            to_address=to_address,
            date=date,
            value_text=value_text,
        )
        identity = _fallback_identity(filtered, target=target)
        signature = _canonical_json(raw_record, description)
        previous_signature = selected_signatures.get(identity)
        if previous_signature is None:
            selected_signatures[identity] = signature
            selected.append(filtered)
        elif previous_signature != signature:
            raise AmbiguousTransferIdentityError(
                "conflicting source objects for fallback transfer identity "
                f"within response for ordinary member {capture_member}"
            )

    return tuple(selected)


def _fallback_identity(
    transfer: _FilteredTransfer,
    *,
    target: TargetToken,
) -> _FallbackIdentity:
    data = transfer.raw_record["data"]
    return (
        target.chain,
        target.token_address,
        data["tx_hash"],
        transfer.from_address,
        transfer.to_address,
        transfer.date,
        transfer.value_text,
    )


def _collect_unique_transfers(
    filtered_by_member: Mapping[str, Sequence[_FilteredTransfer]],
    *,
    target: TargetToken,
) -> tuple[_FilteredTransfer, ...]:
    selected: dict[
        _FallbackIdentity,
        tuple[_FilteredTransfer, str, str],
    ] = {}

    for capture_member, records in filtered_by_member.items():
        for record_index, record in enumerate(records):
            identity = _fallback_identity(record, target=target)
            signature = _canonical_json(
                record.raw_record,
                f"accepted transfer {record_index} for {capture_member}",
            )
            previous = selected.get(identity)
            if previous is None:
                selected[identity] = (record, signature, capture_member)
                continue

            _previous_record, previous_signature, previous_member = previous
            if previous_signature != signature:
                raise AmbiguousTransferIdentityError(
                    "conflicting source objects for fallback transfer identity "
                    f"captured by {previous_member} and {capture_member}"
                )

    return tuple(record for record, _signature, _member in selected.values())


def _verify_normal_pair_counts(
    unique_transfers: Sequence[_FilteredTransfer],
    *,
    target: TargetToken,
    member_index: Mapping[str, _MemberContext],
    edges: Sequence[SubgraphEdge],
    unavailable_members: frozenset[str],
) -> tuple[dict, ...]:
    actual_counts: Counter[tuple[str, str]] = Counter()
    actual_records: dict[tuple[str, str], list[_FilteredTransfer]] = {}
    for transfer in unique_transfers:
        sender = member_index.get(transfer.from_address)
        receiver = member_index.get(transfer.to_address)
        if (
            sender is None
            or receiver is None
            or sender.cluster_index != receiver.cluster_index
            or sender.holder.is_supernode
            or receiver.holder.is_supernode
            or transfer.from_address in unavailable_members
            or transfer.to_address in unavailable_members
        ):
            continue
        pair = (transfer.from_address, transfer.to_address)
        actual_counts[pair] += 1
        actual_records.setdefault(pair, []).append(transfer)

    expected_counts: dict[tuple[str, str], int] = {}
    expected_last_dates: dict[tuple[str, str], int] = {}
    for edge_index, edge in enumerate(edges):
        try:
            from_address = canonicalize_address(
                target.chain,
                edge.from_address,
            )
            to_address = canonicalize_address(target.chain, edge.to_address)
        except (AttributeError, ValueError):
            continue

        sender = member_index.get(from_address)
        receiver = member_index.get(to_address)
        if (
            sender is None
            or receiver is None
            or sender.cluster_index != receiver.cluster_index
            or sender.holder.is_supernode
            or receiver.holder.is_supernode
            or from_address in unavailable_members
            or to_address in unavailable_members
        ):
            continue

        total_transfers = edge.total_transfers
        if type(total_transfers) is not int or total_transfers < 0:
            raise TransferCompletenessError(
                f"directed edge {edge_index} has invalid total_transfers"
            )
        pair = (from_address, to_address)
        previous_total = expected_counts.get(pair)
        if previous_total is not None and previous_total != total_transfers:
            raise TransferCompletenessError(
                "conflicting duplicate directed expectations for "
                f"{from_address} -> {to_address}"
            )
        expected_counts[pair] = total_transfers
        data = edge.raw.get("data") if isinstance(edge.raw, Mapping) else None
        last_date = data.get("last_date") if isinstance(data, Mapping) else None
        if type(last_date) is int and last_date >= 0:
            expected_last_dates[pair] = last_date

    count_drifts: list[dict] = []
    for from_address, to_address in sorted(
        set(actual_counts) | set(expected_counts)
    ):
        pair = (from_address, to_address)
        actual = actual_counts.get((from_address, to_address), 0)
        expected = expected_counts.get((from_address, to_address), 0)
        if actual == expected:
            continue
        last_date = expected_last_dates.get(pair)
        if pair not in expected_counts and actual > 0:
            count_drifts.append(
                {
                    "from_address": from_address,
                    "to_address": to_address,
                    "expected_count": 0,
                    "captured_count": actual,
                    "edge_last_date": None,
                }
            )
            continue
        records = actual_records.get(pair, [])
        captured_at_snapshot = (
            sum(record.date <= last_date for record in records)
            if last_date is not None
            else -1
        )
        if actual > expected and captured_at_snapshot == expected:
            count_drifts.append(
                {
                    "from_address": from_address,
                    "to_address": to_address,
                    "expected_count": expected,
                    "captured_count": actual,
                    "edge_last_date": last_date,
                }
            )
            continue
        raise TransferCompletenessError(
            "directed ordinary transfer count mismatch for "
            f"{from_address} -> {to_address}: expected {expected}, "
            f"captured {actual}"
        )
    return tuple(count_drifts)


def _assign_member_views(
    unique_transfers: Sequence[_FilteredTransfer],
    *,
    target: TargetToken,
    member_index: Mapping[str, _MemberContext],
    unavailable_members: frozenset[str],
) -> dict[str, list[dict]]:
    member_views: dict[str, list[dict]] = {
        context.holder.address: []
        for context in member_index.values()
        if (
            not context.holder.is_supernode
            and context.holder.address not in unavailable_members
        )
    }
    sort_keys: dict[int, tuple[int, _FallbackIdentity]] = {}

    for transfer in unique_transfers:
        raw_record = transfer.raw_record
        sender = member_index.get(transfer.from_address)
        receiver = member_index.get(transfer.to_address)

        if (
            sender is not None
            and sender.holder.address in member_views
        ):
            member_views[sender.holder.address].append(raw_record)
        if (
            receiver is not None
            and receiver.holder.address in member_views
            and (sender is None or receiver.holder.address != sender.holder.address)
        ):
            member_views[receiver.holder.address].append(raw_record)

        sort_keys[id(raw_record)] = (
            -transfer.date,
            _fallback_identity(transfer, target=target),
        )

    for records in member_views.values():
        records.sort(key=lambda record: sort_keys[id(record)])
    return member_views


def _build_documents(
    *,
    target: TargetToken,
    clusters: Sequence[Cluster],
    member_views: Mapping[str, list[dict]],
    captured_at: str,
    unavailable_members: frozenset[str],
) -> tuple[dict, dict[str, dict]]:
    cluster_documents: list[dict] = []
    member_documents: dict[str, dict] = {}

    for cluster in clusters:
        member_summaries: list[dict] = []
        for member_rank, holder in enumerate(cluster.members, start=1):
            try:
                metadata = copy.deepcopy(holder.metadata)
            except Exception:
                raise ValueError(
                    f"metadata for Cluster member {holder.address} "
                    "could not be copied safely"
                ) from None

            summary = {
                "member_rank": member_rank,
                "source_rank": holder.source_rank,
                "address": holder.address,
                "amount": holder.amount,
                "share": holder.share,
                "share_percent": holder.share_percent,
                "is_supernode": holder.is_supernode,
                "metadata": metadata,
            }
            if holder.is_supernode:
                summary.update(
                    {
                        "transfer_details_available": False,
                        "transfer_details_reason": "supernode_not_supported",
                        "transfer_count": 0,
                        "transfer_file": None,
                    }
                )
            elif holder.address in unavailable_members:
                summary.update(
                    {
                        "transfer_details_available": False,
                        "transfer_details_reason": "capture_failed",
                        "transfer_count": 0,
                        "transfer_file": None,
                    }
                )
            else:
                transfers = member_views[holder.address]
                summary.update(
                    {
                        "transfer_details_available": True,
                        "transfer_count": len(transfers),
                        "transfer_file": (
                            "transfers/"
                            f"{safe_path_component(holder.address)}.json"
                        ),
                    }
                )
                member_documents[holder.address] = {
                    "schema_version": "v2",
                    "chain": target.requested_chain,
                    "token_address": target.requested_token_address,
                    "canonical_chain": target.chain,
                    "canonical_token_address": target.token_address,
                    "cluster_rank": cluster.cluster_rank,
                    "member_address": holder.address,
                    "transfer_count": len(transfers),
                    "transfers": transfers,
                }
            member_summaries.append(summary)

        cluster_documents.append(
            {
                "cluster_rank": cluster.cluster_rank,
                "amount": cluster.amount,
                "share": cluster.share,
                "share_percent": cluster.share_percent,
                "member_count": len(cluster.members),
                "members": member_summaries,
            }
        )

    token_document = {
        "schema_version": "v2",
        "chain": target.requested_chain,
        "token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
        "captured_at": captured_at,
        "clusters": cluster_documents,
    }
    _canonical_json(token_document, "token transfer document")
    for address, member_document in member_documents.items():
        _canonical_json(
            member_document,
            f"transfer document for ordinary member {address}",
        )
    return token_document, member_documents


def build_transfer_result(
    payloads_by_member: Mapping[str, object],
    *,
    target: TargetToken,
    clusters: Sequence[Cluster],
    edges: Sequence[SubgraphEdge],
    captured_at: str,
    unavailable_members: Iterable[str] = (),
) -> TransferResult:
    member_index = _build_member_index(clusters, target=target)
    try:
        unavailable_values = tuple(unavailable_members)
        unavailable = frozenset(unavailable_values)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("unavailable members could not be inspected") from None
    ordinary_addresses = {
        address
        for address, context in member_index.items()
        if not context.holder.is_supernode
    }
    if (
        any(type(address) is not str for address in unavailable_values)
        or len(unavailable_values) != len(unavailable)
        or not unavailable.issubset(ordinary_addresses)
    ):
        raise ValueError(
            "unavailable members must be unique ordinary Cluster members"
        )
    payloads = _validated_payloads(
        payloads_by_member,
        member_index,
        unavailable,
    )

    filtered_by_member = {
        address: _filter_member_payload(
            payload,
            capture_member=address,
            target=target,
        )
        for address, payload in payloads.items()
    }
    unique_transfers = _collect_unique_transfers(
        filtered_by_member,
        target=target,
    )
    count_drifts = _verify_normal_pair_counts(
        unique_transfers,
        target=target,
        member_index=member_index,
        edges=edges,
        unavailable_members=unavailable,
    )
    member_views = _assign_member_views(
        unique_transfers,
        target=target,
        member_index=member_index,
        unavailable_members=unavailable,
    )
    token_document, member_documents = _build_documents(
        target=target,
        clusters=clusters,
        member_views=member_views,
        captured_at=captured_at,
        unavailable_members=unavailable,
    )
    return TransferResult(
        token_document=token_document,
        member_documents=member_documents,
        unique_transfer_count=len(unique_transfers),
        transfer_view_count=sum(
            len(document["transfers"])
            for document in member_documents.values()
        ),
        count_drifts=count_drifts,
    )
