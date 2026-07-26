from __future__ import annotations

import json
import math
import random
import re
import struct
import zlib
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, text: str, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if bom:
        text = "\ufeff" + text.lstrip("\ufeff")
    path.write_bytes(text.encode("utf-8"))


def matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    i = open_idx
    while i < len(text):
        c = text[i]
        if in_comment:
            if c == "\n":
                in_comment = False
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == "#":
            in_comment = True
        elif c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"Chave sem fechamento na posição {open_idx}")


def brace_balance(text: str) -> int:
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    for c in text:
        if in_comment:
            if c == "\n":
                in_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == "#":
            in_comment = True
        elif c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("Balanço de chaves negativo")
    return depth


def top_level_blocks(text: str, name_pattern: str) -> List[Tuple[str, int, int, int, int]]:
    rx = re.compile(rf"(?m)^\s*({name_pattern})\s*=\s*\{{")
    matches = list(rx.finditer(text))
    out: List[Tuple[str, int, int, int, int]] = []
    for match in matches:
        # Count braces before the candidate, ignoring strings/comments, to reject nested blocks.
        prefix = text[:match.start()]
        if brace_balance(prefix) != 0:
            continue
        op = text.find("{", match.start(), match.end())
        cl = matching_brace(text, op)
        out.append((match.group(1), match.start(), op, cl, cl + 1))
    return out


def find_direct_field(block: str, key: str) -> Optional[Tuple[int, int]]:
    op = block.find("{")
    cl = matching_brace(block, op)
    rx = re.compile(rf"(?m)^[ \t]*{re.escape(key)}\s*=\s*")
    for m in rx.finditer(block, op + 1, cl):
        if brace_balance(block[op + 1:m.start()]) != 0:
            continue
        start = m.start()
        value_start = m.end()
        while value_start < cl and block[value_start] in " \t":
            value_start += 1
        if value_start < cl and block[value_start] == "{":
            end = matching_brace(block, value_start) + 1
        else:
            nl = block.find("\n", value_start, cl)
            end = cl if nl == -1 else nl
        return start, end
    return None


def replace_direct_fields(block: str, replacements: Dict[str, str]) -> str:
    operations: List[Tuple[int, int, str]] = []
    missing: List[str] = []
    for key, replacement in replacements.items():
        span = find_direct_field(block, key)
        if span:
            operations.append((span[0], span[1], replacement))
        else:
            missing.append(replacement)
    for start, end, replacement in sorted(operations, reverse=True):
        block = block[:start] + replacement + block[end:]
    if missing:
        cl = matching_brace(block, block.find("{"))
        block = block[:cl] + "\n" + "\n".join(missing) + "\n" + block[cl:]
    return block


@dataclass
class StrategicRegionDefinition:
    key: str
    source_file: Path
    block_start: int
    block_end: int
    block: str
    states: List[str]
    capital_province: str
    map_color: str


@dataclass
class StrategicRegionPlan:
    mode: str
    regions: Dict[str, StrategicRegionDefinition]
    assignments: Dict[str, List[str]]
    capitals: Dict[str, str]
    adjacency: Dict[str, Set[str]]
    components: Dict[str, int]
    component_sizes: Counter
    capital_states: Dict[str, str] = field(default_factory=dict)
    state_provinces: Dict[str, List[str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def state_to_region(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for region, members in self.assignments.items():
            for state in members:
                result[state] = region
        return result


def parse_state_provinces(state_dir: Path) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    provinces: Dict[str, List[str]] = {}
    macro_order: Dict[str, List[str]] = defaultdict(list)
    for path in sorted(state_dir.glob("*.txt")):
        if path.name.endswith(".md") or path.name == "99_seas.txt":
            continue
        text = read_text(path)
        for name, start, _op, _cl, end in top_level_blocks(text, r"STATE_[A-Z0-9_]+"):
            block = text[start:end]
            m = re.search(r"\bprovinces\s*=\s*\{([^{}]*)\}", block, re.S)
            values = re.findall(r'"?(x[0-9A-Fa-f]+)"?', m.group(1)) if m else []
            provinces[name] = values
            macro_order[path.stem].append(name)
    return provinces, dict(macro_order)


def parse_strategic_region_definitions(region_dir: Path) -> Tuple[Dict[str, StrategicRegionDefinition], Dict[Path, str]]:
    regions: Dict[str, StrategicRegionDefinition] = {}
    file_texts: Dict[Path, str] = {}
    for path in sorted(region_dir.glob("*.txt")):
        if path.name.endswith(".md") or "water" in path.name.lower():
            continue
        text = read_text(path)
        file_texts[path] = text
        for key, start, _op, _cl, end in top_level_blocks(text, r"region_[a-z0-9_]+"):
            block = text[start:end]
            sm = re.search(r"\bstates\s*=\s*\{([^{}]*)\}", block, re.S)
            if not sm:
                continue
            cm = re.search(r"(?m)^\s*capital_province\s*=\s*\"?(x[0-9A-Fa-f]+)\"?", block)
            mm = re.search(r"(?m)^\s*map_color\s*=\s*(\{[^{}]*\})", block)
            regions[key] = StrategicRegionDefinition(
                key=key,
                source_file=path,
                block_start=start,
                block_end=end,
                block=block,
                states=re.findall(r"\bSTATE_[A-Z0-9_]+\b", sm.group(1)),
                capital_province=cm.group(1) if cm else "",
                map_color=mm.group(1).strip() if mm else "{ 128 128 128 }",
            )
    return regions, file_texts


def _add_edge(adjacency: Dict[str, Set[str]], a: str, b: str) -> None:
    if a == b or not a or not b:
        return
    adjacency[a].add(b)
    adjacency[b].add(a)


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_png_rows(path: Path):
    """Yield RGB scanlines from an 8-bit non-interlaced PNG using only stdlib.

    Victoria 3's provinces.png is a colour-ID map. Keeping this decoder in the
    generator avoids requiring Pillow on the player's machine.
    """
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Arquivo não é um PNG válido: {path}")
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    palette: Optional[List[Tuple[int, int, int]]] = None
    idat = bytearray()
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(">IIBBBBB", payload)
        elif chunk_type == b"PLTE":
            palette = [tuple(payload[i:i + 3]) for i in range(0, len(payload), 3)]
        elif chunk_type == b"IDAT":
            idat.extend(payload)
        elif chunk_type == b"IEND":
            break
    if None in (width, height, bit_depth, color_type, interlace):
        raise ValueError(f"IHDR ausente ou inválido em {path}")
    if bit_depth != 8 or interlace != 0:
        raise ValueError("provinces.png precisa estar em PNG de 8 bits e sem entrelaçamento.")
    if color_type == 2:
        bytes_per_pixel = 3
    elif color_type == 6:
        bytes_per_pixel = 4
    elif color_type == 3:
        bytes_per_pixel = 1
        if not palette:
            raise ValueError("PNG indexado sem paleta.")
    else:
        raise ValueError(f"Formato de cor PNG não suportado em provinces.png: {color_type}")
    stride = width * bytes_per_pixel
    raw = zlib.decompress(bytes(idat))
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise ValueError(f"Tamanho descomprimido inesperado em provinces.png: {len(raw)} != {expected}")
    previous = bytearray(stride)
    offset = 0
    for _y in range(height):
        filter_type = raw[offset]
        source = raw[offset + 1:offset + 1 + stride]
        offset += stride + 1
        row = bytearray(stride)
        if filter_type == 0:
            row[:] = source
        elif filter_type == 1:
            for i, value in enumerate(source):
                left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                row[i] = (value + left) & 0xFF
        elif filter_type == 2:
            for i, value in enumerate(source):
                row[i] = (value + previous[i]) & 0xFF
        elif filter_type == 3:
            for i, value in enumerate(source):
                left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                row[i] = (value + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i, value in enumerate(source):
                left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                above = previous[i]
                upper_left = previous[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                row[i] = (value + _paeth_predictor(left, above, upper_left)) & 0xFF
        else:
            raise ValueError(f"Filtro PNG desconhecido: {filter_type}")
        if color_type == 2:
            yield width, height, [(row[i], row[i + 1], row[i + 2]) for i in range(0, stride, 3)]
        elif color_type == 6:
            yield width, height, [(row[i], row[i + 1], row[i + 2]) for i in range(0, stride, 4)]
        else:
            assert palette is not None
            try:
                yield width, height, [palette[index] for index in row]
            except IndexError as exc:
                raise ValueError("Índice de paleta inválido em provinces.png") from exc
        previous = row


def build_exact_adjacency(
    all_states: Iterable[str],
    provinces: Dict[str, List[str]],
    strait_dir: Path,
    province_map: Path,
) -> Tuple[Dict[str, Set[str]], Dict[str, Tuple[float, float]], Tuple[int, int]]:
    """Build true state adjacency from the province raster and scripted straits."""
    states_order = list(all_states)
    state_index = {state: index for index, state in enumerate(states_order)}
    colour_to_index: Dict[int, int] = {}
    for state, values in provinces.items():
        if state not in state_index:
            continue
        index = state_index[state]
        for province in values:
            try:
                colour_to_index[int(province.removeprefix("x"), 16)] = index
            except ValueError:
                continue
    adjacency_list: List[Set[int]] = [set() for _ in states_order]
    sum_x = [0] * len(states_order)
    sum_y = [0] * len(states_order)
    pixel_count = [0] * len(states_order)
    previous_ids: Optional[List[int]] = None
    width = height = 0
    for y, (width, height, rgb_row) in enumerate(_decode_png_rows(province_map)):
        current_ids = [-1] * width
        left_id = -1
        for x, (red, green, blue) in enumerate(rgb_row):
            current_id = colour_to_index.get((red << 16) | (green << 8) | blue, -1)
            current_ids[x] = current_id
            if current_id >= 0:
                sum_x[current_id] += x
                sum_y[current_id] += y
                pixel_count[current_id] += 1
                if left_id >= 0 and left_id != current_id:
                    adjacency_list[current_id].add(left_id)
                    adjacency_list[left_id].add(current_id)
                if previous_ids is not None:
                    above_id = previous_ids[x]
                    if above_id >= 0 and above_id != current_id:
                        adjacency_list[current_id].add(above_id)
                        adjacency_list[above_id].add(current_id)
            left_id = current_id
        # The Victoria map wraps horizontally at the date line.
        if width > 1 and current_ids[0] >= 0 and current_ids[-1] >= 0 and current_ids[0] != current_ids[-1]:
            a, b = current_ids[0], current_ids[-1]
            adjacency_list[a].add(b)
            adjacency_list[b].add(a)
        previous_ids = current_ids

    missing_pixels = [states_order[i] for i, count in enumerate(pixel_count) if count == 0]
    if missing_pixels:
        raise ValueError(
            "Não encontrei no provinces.png as cores de alguns estados: "
            + ", ".join(missing_pixels[:8])
        )

    adjacency: Dict[str, Set[str]] = {
        state: {states_order[index] for index in adjacency_list[state_index[state]]}
        for state in states_order
    }
    centroids = {
        state: (
            sum_x[state_index[state]] / pixel_count[state_index[state]],
            sum_y[state_index[state]] / pixel_count[state_index[state]],
        )
        for state in states_order
    }

    province_to_state = {
        province.upper(): state
        for state, province_list in provinces.items()
        for province in province_list
    }
    if strait_dir.exists():
        for path in sorted(strait_dir.glob("*.txt")):
            if path.name.endswith(".md"):
                continue
            text = read_text(path)
            for _key, start, _op, _cl, end in top_level_blocks(text, r"[a-z0-9_]+"):
                block = text[start:end]
                a = re.search(r"\bfirst_land_endpoint\s*=\s*(x[0-9A-Fa-f]+)", block)
                b = re.search(r"\bsecond_land_endpoint\s*=\s*(x[0-9A-Fa-f]+)", block)
                if a and b:
                    _add_edge(
                        adjacency,
                        province_to_state.get(a.group(1).upper(), ""),
                        province_to_state.get(b.group(1).upper(), ""),
                    )
    return adjacency, centroids, (width, height)

def connected_components(adjacency: Dict[str, Set[str]]) -> Tuple[Dict[str, int], Counter]:
    component_of: Dict[str, int] = {}
    sizes: Counter = Counter()
    component_id = 0
    for state in sorted(adjacency):
        if state in component_of:
            continue
        queue = [state]
        component_of[state] = component_id
        while queue:
            current = queue.pop()
            sizes[component_id] += 1
            for neighbour in adjacency[current]:
                if neighbour not in component_of:
                    component_of[neighbour] = component_id
                    queue.append(neighbour)
        component_id += 1
    return component_of, sizes


def _distances_from(adjacency: Dict[str, Set[str]], start: str, allowed: Set[str]) -> Dict[str, int]:
    distance = {start: 0}
    queue: deque[str] = deque([start])
    while queue:
        state = queue.popleft()
        for neighbour in adjacency[state]:
            if neighbour in allowed and neighbour not in distance:
                distance[neighbour] = distance[state] + 1
                queue.append(neighbour)
    return distance


def _choose_spread_seeds(
    nodes: List[str],
    count: int,
    adjacency: Dict[str, Set[str]],
    rng: random.Random,
    jitter: float = 0.8,
) -> List[str]:
    if count >= len(nodes):
        return list(nodes)
    allowed = set(nodes)
    first = rng.choice(nodes)
    seeds = [first]
    nearest = _distances_from(adjacency, first, allowed)
    for _ in range(1, count):
        scored: List[Tuple[float, str]] = []
        for node in nodes:
            if node in seeds:
                continue
            distance = nearest.get(node, 0)
            scored.append((distance + rng.random() * jitter, node))
        _score, selected = max(scored)
        seeds.append(selected)
        distances = _distances_from(adjacency, selected, allowed)
        for node in nodes:
            nearest[node] = min(nearest.get(node, 10**9), distances.get(node, 10**9))
    return seeds


def _economic_signature(state: object) -> Tuple[float, float, float, float]:
    arable = math.log1p(max(0, int(getattr(state, "arable_land", 0))))
    resources = sum(getattr(state, "capped", {}).values())
    resources += sum(getattr(resource, "total_amount", 0) for resource in getattr(state, "resources", []))
    resource_score = math.log1p(max(0, resources))
    population = math.log1p(max(0, int(getattr(state, "population", 0))))
    coastal = 1.0 if bool(getattr(state, "coastal", False)) else 0.0
    return arable, resource_score, population / 4.0, coastal * 2.0


def _economic_distance(a: Tuple[float, ...], b: Tuple[float, ...]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _choose_economic_seeds(
    nodes: List[str],
    count: int,
    adjacency: Dict[str, Set[str]],
    states: Dict[str, object],
    rng: random.Random,
) -> List[str]:
    if count >= len(nodes):
        return list(nodes)
    allowed = set(nodes)
    signatures = {node: _economic_signature(states[node]) for node in nodes}
    first = max(
        nodes,
        key=lambda node: sum(signatures[node]) + rng.random() * 1.5,
    )
    seeds = [first]
    nearest_geo = _distances_from(adjacency, first, allowed)
    for _ in range(1, count):
        scored: List[Tuple[float, str]] = []
        for node in nodes:
            if node in seeds:
                continue
            economic_diversity = min(
                _economic_distance(signatures[node], signatures[seed])
                for seed in seeds
            )
            scored.append((nearest_geo.get(node, 0) + economic_diversity * 0.65 + rng.random(), node))
        _score, selected = max(scored)
        seeds.append(selected)
        distance = _distances_from(adjacency, selected, allowed)
        for node in nodes:
            nearest_geo[node] = min(nearest_geo.get(node, 10**9), distance.get(node, 10**9))
    return seeds


def _state_similarity(a: object, b: object) -> float:
    score = 0.0
    if getattr(a, "climate_profile", "") == getattr(b, "climate_profile", ""):
        score += 2.0
    if bool(getattr(a, "coastal", False)) == bool(getattr(b, "coastal", False)):
        score += 0.8
    arable_a = max(0, int(getattr(a, "arable_land", 0)))
    arable_b = max(0, int(getattr(b, "arable_land", 0)))
    score += max(0.0, 1.5 - abs(math.log1p(arable_a) - math.log1p(arable_b)))
    resources_a = set(getattr(a, "capped", {}).keys()) | {r.building for r in getattr(a, "resources", [])}
    resources_b = set(getattr(b, "capped", {}).keys()) | {r.building for r in getattr(b, "resources", [])}
    if resources_a or resources_b:
        score += len(resources_a & resources_b) / max(1, len(resources_a | resources_b)) * 1.5
    return score


def _grow_regions_for_component(
    component_states: List[str],
    region_keys: List[str],
    target_sizes: Dict[str, int],
    adjacency: Dict[str, Set[str]],
    states: Dict[str, object],
    mode: str,
    rng: random.Random,
) -> Dict[str, List[str]]:
    """Create connected, approximately target-sized graph Voronoi cells.

    Additively weighted shortest-path cells stay connected to their seed. The
    iterative biases let undersized cells expand and oversized cells contract
    without the dead-end problem of hard-cap frontier growth.
    """
    if mode == "economic":
        seeds = _choose_economic_seeds(component_states, len(region_keys), adjacency, states, rng)
    else:
        seeds = _choose_spread_seeds(
            component_states,
            len(region_keys),
            adjacency,
            rng,
            jitter=3.0 if mode == "chaos" else 0.8,
        )
    shuffled_keys = list(region_keys)
    rng.shuffle(shuffled_keys)
    seed_by_region = dict(zip(shuffled_keys, seeds))
    allowed = set(component_states)
    distances = {
        region: _distances_from(adjacency, seed, allowed)
        for region, seed in seed_by_region.items()
    }
    # A disconnected distance would mean the component computation and graph disagree.
    for region, distance in distances.items():
        if len(distance) != len(component_states):
            raise ValueError(f"Semente de {region} não alcança todo o componente geográfico.")

    biases = {region: 0.0 for region in shuffled_keys}
    # Region-wide tie breakers are equivalent to tiny bias offsets and therefore
    # preserve graph-Voronoi contiguity.
    tie = {region: rng.random() * 0.01 for region in shuffled_keys}

    best_members: Optional[Dict[str, List[str]]] = None
    best_error = 10**18
    for iteration in range(1600):
        members: Dict[str, List[str]] = {region: [] for region in shuffled_keys}
        for state in component_states:
            owner = min(
                shuffled_keys,
                key=lambda region: distances[region][state] - biases[region] + tie[region],
            )
            members[owner].append(state)
        sizes = {region: len(members[region]) for region in shuffled_keys}
        empty_regions = [region for region in shuffled_keys if sizes[region] == 0]
        error = sum(abs(sizes[region] - target_sizes[region]) for region in shuffled_keys) + len(empty_regions) * 10000
        if error < best_error:
            best_error = error
            best_members = {region: list(values) for region, values in members.items()}
            if error == 0:
                break
        # Guarantee that a vanished cell regains its own seed through the weighted
        # distance rule rather than by forcibly creating a disconnected island.
        for region in empty_regions:
            seed = seed_by_region[region]
            required = max(
                biases[other] - distances[other][seed] + tie[other] - tie[region]
                for other in shuffled_keys if other != region
            )
            biases[region] = max(biases[region], required + 0.25)

        # Larger early movements cross integer-distance thresholds; later movements
        # refine the cells without making them oscillate indefinitely.
        step = max(0.035, 0.42 * (1.0 - iteration / 1700.0))
        for region in shuffled_keys:
            delta = target_sizes[region] - sizes[region]
            biases[region] += step * delta / max(2.0, math.sqrt(target_sizes[region]))
        # Biases are relative; centering keeps numbers bounded.
        mean_bias = sum(biases.values()) / len(biases)
        for region in shuffled_keys:
            biases[region] -= mean_bias

    if best_members is None:
        raise ValueError("Não foi possível formar regiões estratégicas.")
    return best_members


def make_strategic_region_plan(
    state_dir: Path,
    region_dir: Path,
    strait_dir: Path,
    province_map: Path,
    states: Dict[str, object],
    mode: str,
    rng: random.Random,
) -> StrategicRegionPlan:
    regions, _texts = parse_strategic_region_definitions(region_dir)
    provinces, _macro_order = parse_state_provinces(state_dir)
    original_assignments = {key: list(definition.states) for key, definition in regions.items()}
    province_to_state = {province.upper(): state for state, values in provinces.items() for province in values}

    if mode == "keep":
        adjacency: Dict[str, Set[str]] = {state: set() for state in states}
        component_of = {state: index for index, state in enumerate(sorted(states))}
        component_sizes = Counter(component_of.values())
        capitals = {key: definition.capital_province for key, definition in regions.items()}
        capital_states = {
            key: province_to_state.get(capital.upper(), definition.states[0] if definition.states else "")
            for key, (capital, definition) in ((key, (capitals[key], regions[key])) for key in regions)
        }
        return StrategicRegionPlan(
            mode, regions, original_assignments, capitals, adjacency,
            component_of, component_sizes, capital_states, provinces,
            warnings=["Regiões estratégicas originais preservadas."],
        )

    if not province_map.exists():
        raise FileNotFoundError(
            "Para aleatorizar regiões estratégicas com contiguidade real, o gerador precisa de "
            f"{province_map}. Selecione a pasta completa da instalação do Victoria 3, e não um pacote parcial."
        )

    adjacency, centroids, map_size = build_exact_adjacency(
        states.keys(), provinces, strait_dir, province_map
    )
    component_of, component_sizes = connected_components(adjacency)
    component_members: Dict[int, List[str]] = defaultdict(list)
    for state, component_id in component_of.items():
        component_members[component_id].append(state)

    # Each preserved region key is anchored to the land component containing the
    # plurality of its vanilla states. Small disconnected islands are attached only
    # after the mainland cells have been grown, so they can never create enclaves.
    regions_by_component: Dict[int, List[str]] = defaultdict(list)
    old_region_by_state: Dict[str, str] = {}
    for key, definition in regions.items():
        for state in definition.states:
            old_region_by_state[state] = key
        counts = Counter(component_of[s] for s in definition.states if s in component_of)
        if counts:
            best_count = max(counts.values())
            tied = [component for component, count in counts.items() if count == best_count]
            chosen = min(
                tied,
                key=lambda component: (
                    -component_sizes[component],
                    min(component_members[component]),
                ),
            )
            regions_by_component[chosen].append(key)

    assignments: Dict[str, List[str]] = {}
    warnings: List[str] = [
        "A contiguidade foi calculada diretamente pelo raster map_data/provinces.png e pelos estreitos do jogo."
    ]
    satellite_components: List[int] = []
    for component_id, component_state_count in sorted(component_sizes.items()):
        component_states = sorted(component_members[component_id])
        keys = list(regions_by_component.get(component_id, []))
        if not keys:
            satellite_components.append(component_id)
            continue
        if len(keys) > component_state_count:
            raise ValueError(
                f"O componente geográfico {component_id} possui {component_state_count} estados, "
                f"mas recebeu {len(keys)} regiões."
            )
        original_sizes = [len(regions[key].states) for key in keys]
        weights = [max(1, size) for size in original_sizes]
        raw = [component_state_count * weight / sum(weights) for weight in weights]
        sizes = [max(1, int(math.floor(value))) for value in raw]
        while sum(sizes) < component_state_count:
            index = max(
                range(len(sizes)),
                key=lambda i: raw[i] - sizes[i] + rng.random() * 1e-6,
            )
            sizes[index] += 1
        while sum(sizes) > component_state_count:
            candidates = [i for i in range(len(sizes)) if sizes[i] > 1]
            if not candidates:
                raise ValueError(f"Não foi possível balancear o componente geográfico {component_id}.")
            index = max(candidates, key=lambda i: sizes[i] - raw[i])
            sizes[index] -= 1
        rng.shuffle(sizes)
        target_sizes = dict(zip(keys, sizes))
        grown = _grow_regions_for_component(
            component_states, keys, target_sizes, adjacency, states, mode, rng
        )
        assignments.update(grown)

    # Attach every island/isolated component as a whole. Preference goes to the new
    # region that inherited most of the island's vanilla strategic region; geographic
    # distance is only a fallback. A conceptual sea edge is recorded for validation.
    width, _height = map_size
    attached_islands = 0
    for component_id in satellite_components:
        members = sorted(component_members[component_id])
        old_counts = Counter(old_region_by_state.get(state, "") for state in members)
        candidate_scores: Counter = Counter()
        for new_region, new_members in assignments.items():
            for state in new_members:
                old_key = old_region_by_state.get(state, "")
                if old_key:
                    candidate_scores[new_region] += old_counts.get(old_key, 0)
        if candidate_scores and max(candidate_scores.values()) > 0:
            best_value = max(candidate_scores.values())
            candidates = [region for region, value in candidate_scores.items() if value == best_value]
        else:
            candidates = list(assignments)
        island_anchor = max(
            members,
            key=lambda state: (
                int(getattr(states[state], "province_count", 1)),
                int(getattr(states[state], "population", 0)),
            ),
        )
        ix, iy = centroids[island_anchor]

        def distance_to_region(region: str) -> Tuple[float, str]:
            best_distance = float("inf")
            best_state = assignments[region][0]
            for state in assignments[region]:
                sx, sy = centroids[state]
                dx = abs(ix - sx)
                dx = min(dx, max(0.0, width - dx))
                distance = dx * dx + (iy - sy) * (iy - sy)
                if distance < best_distance:
                    best_distance = distance
                    best_state = state
            return best_distance, best_state

        chosen_region = min(candidates, key=lambda region: distance_to_region(region)[0])
        _distance, attachment_state = distance_to_region(chosen_region)
        assignments[chosen_region].extend(members)
        _add_edge(adjacency, island_anchor, attachment_state)
        attached_islands += 1

    state_to_region = {state: region for region, members in assignments.items() for state in members}
    missing = set(states) - set(state_to_region)
    duplicate_count = sum(len(values) for values in assignments.values()) - len(state_to_region)
    if missing or duplicate_count:
        raise ValueError(
            f"Plano regional inválido: faltantes={len(missing)}, duplicações={duplicate_count}"
        )
    if set(assignments) != set(regions):
        absent_keys = set(regions) - set(assignments)
        raise ValueError(f"Regiões sem atribuição: {sorted(absent_keys)[:5]}")

    capitals: Dict[str, str] = {}
    capital_states: Dict[str, str] = {}
    for key, members in assignments.items():
        selected = max(
            members,
            key=lambda state: (
                int(getattr(states[state], "province_count", 1)),
                int(getattr(states[state], "population", 0)),
                int(getattr(states[state], "arable_land", 0)),
                rng.random(),
            ),
        )
        capital_states[key] = selected
        province_list = provinces.get(selected, [])
        if not province_list:
            capitals[key] = regions[key].capital_province
            warnings.append(
                f"{key}: sem província analisável no estado-capital; capital antiga preservada."
            )
        else:
            capitals[key] = province_list[0]
    if attached_islands:
        warnings.append(
            f"{attached_islands} componentes insulares foram mantidos inteiros e ligados à região geograficamente mais coerente."
        )

    return StrategicRegionPlan(
        mode, regions, assignments, capitals, adjacency, component_of,
        component_sizes, capital_states, provinces, warnings,
    )

def apply_strategic_region_plan(states: Dict[str, object], plan: StrategicRegionPlan) -> Tuple[Dict[str, List[str]], List[str]]:
    sequence = list(plan.regions)
    for region, members in plan.assignments.items():
        for index, state in enumerate(members):
            if state in states:
                setattr(states[state], "strategic_region", region)
                setattr(states[state], "region_order", index)
    return {key: list(plan.assignments[key]) for key in sequence}, sequence


def _state_display_name(state_key: str) -> str:
    words = state_key.removeprefix("STATE_").split("_")
    small = {"OF", "THE", "AND", "DE", "DA", "DO", "DOS", "DAS"}
    rendered = []
    for index, word in enumerate(words):
        if index > 0 and word in small:
            rendered.append(word.lower())
        else:
            rendered.append(word.capitalize())
    return " ".join(rendered)


def _unescape_localization_value(value: str) -> str:
    return value.replace(r'\"', '"').replace(r'\n', ' ').strip()


def load_localized_state_names(base: Path, language: str, state_keys: Set[str]) -> Dict[str, str]:
    """Read the installed game's localized state names for one language.

    Only the requested keys are retained, which keeps this fast even when the
    installation contains many localization files. Files are processed in
    lexical order and later definitions replace earlier ones, matching the
    practical overwrite behaviour used by the game.
    """
    result: Dict[str, str] = {}
    language_dir = base / "localization" / language
    if not language_dir.exists():
        return result
    wanted = set(state_keys)
    line_re = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*:\s*\d*\s*"((?:\\.|[^"\\])*)"')
    for path in sorted(language_dir.rglob("*.yml")):
        try:
            text = read_text(path)
        except (OSError, UnicodeError):
            continue
        for line in text.splitlines():
            match = line_re.match(line)
            if not match:
                continue
            key = match.group(1)
            if key in wanted:
                result[key] = _unescape_localization_value(match.group(2))
    return result


REGION_NAME_TEMPLATES: Dict[str, str] = {
    "english": "{state}",
    "braz_por": "{state}",
    "french": "{state}",
    "german": "{state}",
    "spanish": "{state}",
    "russian": "{state}",
    "polish": "{state}",
    "turkish": "{state}",
    "japanese": "{state}",
    "simp_chinese": "{state}",
    "korean": "{state}",
}


def write_strategic_region_localization(plan: StrategicRegionPlan, base: Path, mod_root: Path) -> None:
    """Write region-name overrides for every supported game language.

    Strategic-region keys are vanilla keys, so their localization must be put
    in localization/<language>/replace. Otherwise the base-game entry can win
    the load order and the English fallback may be shown in non-English games.
    """
    capital_states = {
        region: (plan.capital_states.get(region) or plan.assignments[region][0])
        for region in plan.regions
    }
    wanted_states = set(capital_states.values())

    languages = set(REGION_NAME_TEMPLATES)
    localization_root = base / "localization"
    if localization_root.exists():
        languages.update(path.name for path in localization_root.iterdir() if path.is_dir())

    for language in sorted(languages):
        template = REGION_NAME_TEMPLATES.get(language, REGION_NAME_TEMPLATES["english"])
        localized_states = load_localized_state_names(base, language, wanted_states)
        lines = [f"l_{language}:"]
        for region in plan.regions:
            state_key = capital_states[region]
            display = localized_states.get(state_key) or _state_display_name(state_key)
            display = display.replace('"', r'\"')
            lines.append(f' {region}:0 "{template.format(state=display)}"')

        # Remove the old non-replace file created by v6.1 to avoid duplicate keys.
        old_path = mod_root / f"localization/{language}/bwg_strategic_regions_l_{language}.yml"
        if old_path.exists():
            old_path.unlink()
        write_text(
            mod_root / f"localization/{language}/replace/bwg_strategic_regions_l_{language}.yml",
            "\n".join(lines) + "\n",
            bom=True,
        )


def write_strategic_region_overrides(plan: StrategicRegionPlan, base: Path, mod_root: Path) -> None:
    by_file: Dict[Path, List[StrategicRegionDefinition]] = defaultdict(list)
    for definition in plan.regions.values():
        by_file[definition.source_file].append(definition)
    for source_file, definitions in by_file.items():
        text = read_text(source_file)
        operations: List[Tuple[int, int, str]] = []
        for definition in definitions:
            members = plan.assignments[definition.key]
            wrapped = []
            line: List[str] = []
            for state in members:
                line.append(state)
                if len(line) >= 8:
                    wrapped.append("\t\t" + " ".join(line))
                    line = []
            if line:
                wrapped.append("\t\t" + " ".join(line))
            states_field = "\tstates = {\n" + "\n".join(wrapped) + "\n\t}"
            capital_field = f"\tcapital_province = {plan.capitals[definition.key]}"
            new_block = replace_direct_fields(
                definition.block,
                {"states": states_field, "capital_province": capital_field},
            )
            operations.append((definition.block_start, definition.block_end, new_block))
        for start, end, replacement in sorted(operations, reverse=True):
            text = text[:start] + replacement + text[end:]
        if brace_balance(text) != 0:
            raise ValueError(f"Região estratégica com chaves inválidas: {source_file.name}")
        write_text(mod_root / source_file.relative_to(base), text, bom=True)
    write_strategic_region_localization(plan, base, mod_root)


CENTER_TYPES = ("knowledge", "commerce", "industry", "military", "agriculture")


def _center_score(state: object, center_type: str) -> float:
    population = max(1, int(getattr(state, "target_population", 0) or getattr(state, "population", 0)))
    pop_score = math.log10(population + 10)
    arable = max(0, int(getattr(state, "new_arable_land", 0) or getattr(state, "arable_land", 0)))
    visible_resources = sum(getattr(state, "new_capped", {}).values())
    visible_resources += sum(getattr(resource, "discovered_amount", 0) for resource in getattr(state, "new_resources", []))
    province_count = max(1, int(getattr(state, "province_count", 1)))
    coastal = bool(getattr(state, "coastal", False))
    if center_type == "knowledge":
        return pop_score * 2.6 + math.log1p(arable) * 0.3
    if center_type == "commerce":
        return pop_score * 1.7 + (6.0 if coastal else -3.0) + math.log1p(arable) * 0.2
    if center_type == "industry":
        return pop_score * 1.8 + math.log1p(visible_resources) * 2.0
    if center_type == "military":
        return pop_score * 1.5 + math.log1p(province_count) * 2.5 + (0.7 if coastal else 0.0)
    if center_type == "agriculture":
        return math.log1p(arable) * 3.0 + pop_score * 0.8
    return pop_score


def select_civilization_centers(
    states: Dict[str, object], mode: str, rng: random.Random
) -> Dict[str, str]:
    if mode == "off":
        return {}
    per_type = 7 if mode == "sparse" else 12
    states_by_region: Dict[str, List[str]] = defaultdict(list)
    for name, state in states.items():
        states_by_region[getattr(state, "strategic_region", "region_unassigned")].append(name)
    regions = [region for region, members in states_by_region.items() if members]
    selected: Dict[str, str] = {}
    used: Set[str] = set()
    for center_type in CENTER_TYPES:
        ordered_regions = list(regions)
        rng.shuffle(ordered_regions)
        # Large regions receive a small chance of a second pass without letting one continent dominate.
        region_cycle = ordered_regions + sorted(regions, key=lambda r: len(states_by_region[r]), reverse=True)
        chosen_count = 0
        for region in region_cycle:
            candidates = [s for s in states_by_region[region] if s not in used]
            if center_type == "commerce":
                coastal_candidates = [s for s in candidates if getattr(states[s], "coastal", False)]
                if coastal_candidates:
                    candidates = coastal_candidates
            if not candidates:
                continue
            best = max(candidates, key=lambda s: _center_score(states[s], center_type) + rng.random() * 1.5)
            selected[best] = center_type
            used.add(best)
            chosen_count += 1
            if chosen_count >= per_type:
                break
    return selected


def write_center_files(mod_root: Path, centers: Dict[str, str]) -> None:
    modifier_values = {
        "knowledge": "\tstate_education_access_add = 0.05\n\tbuilding_university_throughput_add = 0.05",
        "commerce": "\tstate_infrastructure_mult = 0.05\n\tbuilding_port_throughput_add = 0.05",
        "industry": "\tbuilding_group_bg_manufacturing_throughput_add = 0.05\n\tbuilding_group_bg_mining_throughput_add = 0.03",
        "military": "\tbuilding_barrack_throughput_add = 0.05\n\tbuilding_naval_administration_throughput_add = 0.03",
        "agriculture": "\tbuilding_group_bg_agriculture_throughput_add = 0.05\n\tbuilding_group_bg_plantations_throughput_add = 0.03",
    }
    text = ["# Generated by Randomised World History Generator v6.10"]
    for center_type in CENTER_TYPES:
        text.extend([f"bwg_center_{center_type} = {{", modifier_values[center_type], "}", ""])
    write_text(mod_root / "common/static_modifiers/99_bwg_civilization_centers.txt", "\n".join(text), bom=True)

    labels_pt = {
        "knowledge": ("Centro de Conhecimento", "Um polo regional de educação, pesquisa e formação especializada."),
        "commerce": ("Centro Comercial", "Um polo natural de circulação, portos e infraestrutura mercantil."),
        "industry": ("Centro Industrial", "Um núcleo regional de manufaturas e extração organizada."),
        "military": ("Centro Militar", "Uma tradição regional de administração e organização militar."),
        "agriculture": ("Centro Agrícola", "Uma região de agricultura comercial e produção rural especializada."),
    }
    labels_en = {
        "knowledge": ("Center of Knowledge", "A regional hub of education, research and specialized training."),
        "commerce": ("Commercial Center", "A natural hub of circulation, ports and mercantile infrastructure."),
        "industry": ("Industrial Center", "A regional nucleus of manufacturing and organized extraction."),
        "military": ("Military Center", "A regional tradition of military administration and organization."),
        "agriculture": ("Agricultural Center", "A region of commercial agriculture and specialized rural production."),
    }
    for language, labels in (("braz_por", labels_pt), ("english", labels_en)):
        lines = [f"l_{language}:"]
        for key, (name, description) in labels.items():
            lines.append(f' bwg_center_{key}:0 "{name}"')
            lines.append(f' bwg_center_{key}_desc:0 "{description}"')
        write_text(
            mod_root / f"localization/{language}/bwg_world_history_l_{language}.yml",
            "\n".join(lines) + "\n",
            bom=True,
        )


def parse_company_definitions(base: Path) -> Tuple[Dict[str, List[str]], List[str]]:
    common = base / "common"
    candidate_dirs = [
        common / "company_types",
        common / "companies",
        common / "company_definitions",
    ]
    candidate_dirs.extend(
        path for path in common.iterdir()
        if path.is_dir() and "company" in path.name.lower() and path not in candidate_dirs
    )
    companies: Dict[str, List[str]] = {}
    warnings: List[str] = []
    for directory in candidate_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.txt")):
            if path.name.endswith(".md"):
                continue
            text = read_text(path)
            for key, start, _op, _cl, end in top_level_blocks(text, r"company_[a-z0-9_]+"):
                block = text[start:end]
                buildings = sorted(set(re.findall(r"\bbuilding_[a-z0-9_]+\b", block)))
                companies[key] = buildings
    if not companies:
        warnings.append("Definições de companhias não foram encontradas no pacote de dados; o módulo manterá apenas a remoção segura das companhias históricas detectadas.")
    return companies, warnings


def parse_historical_company_keys(base: Path) -> Set[str]:
    result: Set[str] = set()
    country_dir = base / "common/history/countries"
    if country_dir.exists():
        for path in country_dir.glob("*.txt"):
            result.update(re.findall(r"add_company\s*=\s*company_type:(company_[a-z0-9_]+)", read_text(path)))
    return result


def _company_category(buildings: Sequence[str]) -> str:
    joined = " ".join(buildings)
    if any(token in joined for token in ("mine", "oil_rig", "gold_field", "rubber_plantation", "logging_camp")):
        return "mining"
    if any(token in joined for token in ("port", "shipyard", "fishing", "whaling", "naval")):
        return "maritime"
    if any(token in joined for token in ("farm", "plantation", "ranch", "food_industry", "vineyard")):
        return "agrarian"
    if any(token in joined for token in ("mill", "manufactory", "workshop", "factory", "industry", "glassworks", "steel")):
        return "protoindustrial"
    return "generic"


def parse_diplomatic_action_keys(base: Path) -> List[str]:
    directory = base / "common/diplomatic_actions"
    if not directory.exists():
        return []
    keys: Set[str] = set()
    for path in directory.rglob("*.txt"):
        if path.name.endswith(".md"):
            continue
        text = read_text(path)
        for key, _start, _op, _cl, _end in top_level_blocks(text, r"[a-z][a-z0-9_]+"):
            keys.add(key)
    safe_tokens = (
        "alliance", "defensive_pact", "trade_agreement", "investment_agreement",
        "military_access", "guarantee_independence", "non_aggression",
    )
    return sorted(key for key in keys if any(token == key or token in key for token in safe_tokens))


def strip_historical_diplomacy(base: Path, mod_root: Path) -> int:
    source_dir = base / "common/history/countries"
    if not source_dir.exists():
        return 0
    removed = 0
    for source in sorted(source_dir.glob("*.txt")):
        target = mod_root / source.relative_to(base)
        text = read_text(target if target.exists() else source)
        while True:
            match = re.search(r"(?m)^[ \t]*create_diplomatic_pact\s*=\s*\{", text)
            if not match:
                break
            op = text.find("{", match.start(), match.end())
            end = matching_brace(text, op) + 1
            if end < len(text) and text[end] == "\r":
                end += 1
            if end < len(text) and text[end] == "\n":
                end += 1
            text = text[:match.start()] + text[end:]
            removed += 1
        # Remove simple historical relation assignments without touching unrelated effects.
        text, count = re.subn(r"(?m)^[ \t]*set_relations\s*=\s*[^\n]+\r?\n", "", text)
        removed += count
        if brace_balance(text) != 0:
            raise ValueError(f"Remoção de diplomacia desequilibrou {source.name}")
        write_text(target, text, bom=True)
    return removed


def patch_on_actions(mod_root: Path, dynamic_companies: bool, remnant_cleanup: bool=True) -> None:
    path = mod_root / "common/on_actions/01_random_stuff.txt"
    text = read_text(path)
    marker = "remove_all_buildings = {"
    start = text.find(marker)
    if start == -1:
        raise ValueError("Não encontrei remove_all_buildings no on_action do mod-base.")
    op = text.find("{", start)
    cl = matching_brace(text, op)
    block = text[start:cl + 1]
    # Clear historical companies/ownership containers before any building is
    # removed and recreated.  This prevents old imperial ownership links from
    # attaching themselves to the new local economy.
    if "bwg_pre_building_cleanup = yes" not in block:
        effect_pos = block.find("effect = {")
        effect_open = block.find("{", effect_pos)
        insert_at = effect_open + 1
        pre = "\n\t\tbwg_pre_building_cleanup = yes"
        if remnant_cleanup:
            pre += "\n\t\tbwg_cleanup_historical_remnants_effect = yes"
        block = block[:insert_at] + pre + block[insert_at:]
    if "bwg_world_history_setup = yes" not in block:
        block_cl = matching_brace(block, block.find("{"))
        block = block[:block_cl] + "\n\tbwg_world_history_setup = yes\n" + block[block_cl:]
        text = text[:start] + block + text[cl + 1:]
    if dynamic_companies and "on_yearly_pulse_country" not in text:
        text += """

on_yearly_pulse_country = {
    on_actions = {
        bwg_dynamic_company_yearly
    }
}

bwg_dynamic_company_yearly = {
    effect = {
        bwg_dynamic_company_pulse = yes
    }
}
"""
    if brace_balance(text) != 0:
        raise ValueError("On-actions da v6.8 ficaram com chaves inválidas.")
    write_text(path, text, bom=True)


def _remove_company_lines(company_keys: Sequence[str], indent: str = "        ") -> List[str]:
    return [f"{indent}safe_remove_company = {{ COMPANY_KEY = {key} }}" for key in company_keys]


def _company_trigger(company: str, buildings: Sequence[str], category: str, indent: str) -> List[str]:
    archetype_var = {
        "agrarian": "bwg_arch_agrarian",
        "mining": "bwg_arch_mining",
        "maritime": "bwg_arch_maritime",
        "protoindustrial": "bwg_arch_protoindustrial",
        "generic": "bwg_arch_protoindustrial",
    }[category]
    lines = [
        f"{indent}trigger = {{",
        f"{indent}    has_variable = {archetype_var}",
        f"{indent}    NOT = {{ has_company = company_type:{company} }}",
        f"{indent}    NOT = {{ any_country = {{ has_company = company_type:{company} }} }}",
    ]
    if buildings:
        lines.append(f"{indent}    OR = {{")
        for building in buildings[:8]:
            lines.append(f"{indent}        country_has_building_type_levels = {{")
            lines.append(f"{indent}            target = bt:{building}")
            lines.append(f"{indent}            value >= 5")
            lines.append(f"{indent}        }}")
        lines.append(f"{indent}    }}")
    lines.extend([f"{indent}}}", f"{indent}add_company = company_type:{company}"])
    return lines


def _company_random_list(companies: Dict[str, List[str]], indent: str = "        ") -> List[str]:
    lines = [f"{indent}random_list = {{", f"{indent}    40 = {{ }}"]
    for company in sorted(companies):
        buildings = companies[company]
        if not buildings:
            continue
        category = _company_category(buildings)
        lines.append(f"{indent}    1 = {{")
        lines.extend(_company_trigger(company, buildings, category, indent + "        "))
        lines.append(f"{indent}    }}")
    lines.append(f"{indent}}}")
    return lines


def write_world_history_scripts(
    base: Path,
    mod_root: Path,
    options: Dict[str, str],
    centers: Dict[str, str],
    company_definitions: Dict[str, List[str]],
    historical_company_keys: Set[str],
    diplomatic_actions: Sequence[str],
) -> List[str]:
    warnings: List[str] = []
    company_mode = options.get("companies", "natural_dynamic")
    needs_mode = options.get("strategic_needs", "natural")
    diplomacy_mode = options.get("diplomacy", "natural_relations")
    all_company_keys = sorted(set(company_definitions) | set(historical_company_keys))
    dynamic_companies = company_mode == "natural_dynamic" and bool(company_definitions)

    lines: List[str] = [
        "# Generated by Randomised World — World History Generator v6.10",
        "",
        "bwg_assign_strategic_need = {",
        "    remove_variable = bwg_need_food",
        "    remove_variable = bwg_need_tools",
        "    remove_variable = bwg_need_coal",
        "    remove_variable = bwg_need_raw_materials",
        "    remove_variable = bwg_need_population",
    ]
    if needs_mode != "off":
        lines.extend([
            "    if = { limit = { has_variable = bwg_arch_agrarian } set_variable = bwg_need_tools }",
            "    else_if = { limit = { has_variable = bwg_arch_mining } set_variable = bwg_need_food }",
            "    else_if = { limit = { has_variable = bwg_arch_maritime } set_variable = bwg_need_coal }",
            "    else_if = { limit = { has_variable = bwg_arch_protoindustrial } set_variable = bwg_need_raw_materials }",
            "    else = { set_variable = bwg_need_population }",
        ])
        if needs_mode == "strong":
            lines.extend([
                "    random_list = {",
                "        7 = { }",
                "        1 = { remove_variable = bwg_need_tools set_variable = bwg_need_food }",
                "        1 = { remove_variable = bwg_need_food set_variable = bwg_need_coal }",
                "        1 = { remove_variable = bwg_need_coal set_variable = bwg_need_raw_materials }",
                "    }",
            ])
    lines.extend(["}", ""])

    lines.extend(["bwg_apply_civilization_centers = {"])
    for state, center_type in sorted(centers.items()):
        lines.append(f"    s:{state} = {{ add_modifier = {{ name = bwg_center_{center_type} months = -1 }} }}")
    lines.extend(["}", ""])

    lines.extend(["bwg_remove_initial_companies = {", "    every_country = {"])
    lines.extend(_remove_company_lines(all_company_keys, indent="        "))
    lines.extend(["    }", "}", ""])

    lines.extend(["bwg_try_add_company = {"])
    if company_definitions:
        lines.extend(_company_random_list(company_definitions, indent="    "))
    lines.extend(["}", ""])

    lines.extend(["bwg_pre_building_cleanup = {", "    bwg_remove_initial_companies = yes", "}", ""])

    lines.extend(["bwg_setup_companies = {"])
    if company_mode != "keep":
        lines.append("    bwg_remove_initial_companies = yes")
    if company_mode == "balanced_initial" and company_definitions:
        lines.extend([
            "    every_country = {",
            "        limit = { NOT = { is_country_type = decentralized } num_companies < 1 }",
            "        random_list = { 3 = { } 1 = { bwg_try_add_company = yes } }",
            "    }",
        ])
    elif company_mode == "natural_dynamic" and company_definitions:
        lines.extend([
            "    every_country = {",
            "        limit = { NOT = { is_country_type = decentralized } num_companies < 1 }",
            "        random_list = { 11 = { } 1 = { bwg_try_add_company = yes } }",
            "    }",
        ])
    lines.extend(["}", ""])

    lines.extend(["bwg_dynamic_company_pulse = {"])
    if dynamic_companies:
        lines.extend([
            "    if = {",
            "        limit = { NOT = { is_country_type = decentralized } num_companies < 2 }",
            "        random_list = { 99 = { } 1 = { bwg_try_add_company = yes } }",
            "    }",
        ])
    lines.extend(["}", ""])

    # Relations are sparse and based on structural complementarity. No crisis or resource-rush module is generated.
    lines.extend([
        "bwg_set_positive_relation_with_saved_partner = {",
        "    set_relations = { country = scope:bwg_partner value = 35 }",
        "}",
        "",
        "bwg_generate_natural_relations = {",
        "    every_country = {",
        "        limit = { NOT = { is_country_type = decentralized } }",
        "        save_scope_as = bwg_origin",
        "        if = {",
        "            limit = { has_variable = bwg_need_food }",
        "            random_country = { limit = { NOT = { scope:bwg_origin ?= this } has_variable = bwg_arch_agrarian } save_scope_as = bwg_partner }",
        "        }",
        "        else_if = {",
        "            limit = { has_variable = bwg_need_tools }",
        "            random_country = { limit = { NOT = { scope:bwg_origin ?= this } has_variable = bwg_arch_protoindustrial } save_scope_as = bwg_partner }",
        "        }",
        "        else_if = {",
        "            limit = { has_variable = bwg_need_coal }",
        "            random_country = { limit = { NOT = { scope:bwg_origin ?= this } has_variable = bwg_arch_mining } save_scope_as = bwg_partner }",
        "        }",
        "        else_if = {",
        "            limit = { has_variable = bwg_need_raw_materials }",
        "            random_country = { limit = { NOT = { scope:bwg_origin ?= this } OR = { has_variable = bwg_arch_agrarian has_variable = bwg_arch_mining } } save_scope_as = bwg_partner }",
        "        }",
        "        else = {",
        "            random_country = { limit = { NOT = { scope:bwg_origin ?= this } has_variable = bwg_arch_protoindustrial } save_scope_as = bwg_partner }",
        "        }",
        "        if = { limit = { exists = scope:bwg_partner } bwg_set_positive_relation_with_saved_partner = yes }",
        "        random_country = {",
        "            limit = { NOT = { scope:bwg_origin ?= this } }",
        "            save_scope_as = bwg_competitor",
        "        }",
        "        if = { limit = { exists = scope:bwg_competitor } set_relations = { country = scope:bwg_competitor value = -20 } }",
        "    }",
        "}",
        "",
    ])

    lines.extend(["bwg_generate_sparse_pacts = {"])
    if diplomatic_actions:
        safe = diplomatic_actions[:6]
        lines.extend([
            "    every_country = {",
            "        limit = { NOT = { is_country_type = decentralized } }",
            "        save_scope_as = bwg_pact_origin",
            "        random_list = {",
            "            9 = { }",
            "            1 = {",
            "                random_country = {",
            "                    limit = { NOT = { scope:bwg_pact_origin ?= this } NOT = { is_country_type = decentralized } }",
            "                    save_scope_as = bwg_pact_partner",
            "                }",
            "                if = {",
            "                    limit = { exists = scope:bwg_pact_partner }",
            "                    random_list = {",
        ])
        for action in safe:
            lines.append(f"                        1 = {{ create_diplomatic_pact = {{ country = scope:bwg_pact_partner type = {action} }} }}")
        lines.extend([
            "                    }",
            "                }",
            "            }",
            "        }",
            "    }",
        ])
    lines.extend(["}", ""])

    lines.extend([
        "bwg_world_history_setup = {",
        "    every_country = { bwg_assign_strategic_need = yes }",
        "    bwg_apply_civilization_centers = yes",
        "    bwg_setup_companies = yes",
    ])
    if diplomacy_mode in {"natural_relations", "natural_pacts"}:
        lines.append("    bwg_generate_natural_relations = yes")
    if diplomacy_mode == "natural_pacts" and diplomatic_actions:
        lines.append("    bwg_generate_sparse_pacts = yes")
    lines.extend(["}", ""])

    script = "\n".join(lines)
    if brace_balance(script) != 0:
        raise ValueError("Script de história procedural ficou com chaves inválidas.")
    write_text(mod_root / "common/scripted_effects/99_bwg_world_history.txt", script, bom=True)
    write_center_files(mod_root, centers)
    patch_on_actions(mod_root, dynamic_companies, options.get("historical_remnants", "dissolve") != "preserve")

    if company_mode in {"balanced_initial", "natural_dynamic"} and not company_definitions:
        warnings.append("Companhias: nenhuma definição completa foi localizada; companhias históricas são removíveis, mas a geração econômica foi desativada nesta execução.")
    if diplomacy_mode == "natural_pacts" and not diplomatic_actions:
        warnings.append("Diplomacia: tipos de pactos seguros não foram localizados; o modo foi reduzido a relações naturais.")
    return warnings


def write_world_history_report(
    mod_root: Path,
    states: Dict[str, object],
    options: Dict[str, str],
    seed: int,
    plan: StrategicRegionPlan,
    centers: Dict[str, str],
    company_count: int,
    diplomatic_action_count: int,
    warnings: Sequence[str],
) -> None:
    names_pt = {
        "knowledge": "conhecimento",
        "commerce": "comércio",
        "industry": "indústria",
        "military": "organização militar",
        "agriculture": "agricultura",
    }
    lines = [
        "RANDOMISED WORLD — HISTÓRIA PROCEDURAL v6.8",
        f"Seed: {seed}",
        "",
        "CONFIGURAÇÃO",
        json.dumps(options, ensure_ascii=False, indent=2),
        "",
        "REGIÕES ESTRATÉGICAS",
    ]
    for key, members in plan.assignments.items():
        population = sum(int(getattr(states[state], "target_population", 0)) for state in members if state in states)
        coastal = sum(bool(getattr(states[state], "coastal", False)) for state in members if state in states)
        lines.append(f"- {key}: {len(members)} estados, {coastal} costeiros, população-alvo {population:,}; capital {plan.capitals.get(key, 'n/a')}")
        lines.append("  " + ", ".join(members))
    lines.extend(["", "CENTROS DE CIVILIZAÇÃO"])
    if centers:
        for state, center_type in sorted(centers.items(), key=lambda item: (item[1], item[0])):
            lines.append(f"- {state}: centro de {names_pt[center_type]}")
    else:
        lines.append("- Desativados.")
    lines.extend([
        "",
        "MÓDULOS PROCEDURAIS",
        f"- Definições econômicas de companhias detectadas: {company_count}",
        f"- Tipos seguros de pacto diplomático detectados: {diplomatic_action_count}",
        "- Necessidades estratégicas ligam os arquétipos econômicos à diplomacia e à formação de companhias.",
        "- A geografia física do mapa é preservada; população, economia e desenvolvimento humano podem ser redistribuídos.",
        "- Não foram incluídos módulos de corrida por recursos nem de crises regionais.",
        "",
        "LIMITAÇÕES",
        "- A malha de contiguidade das regiões estratégicas é extraída diretamente de map_data/provinces.png; ilhas são anexadas como blocos inteiros.",
        "- Companhias e relações são efetivadas após a formação dos países no início da campanha; o relatório descreve a lógica, não a lista final de proprietários.",
    ])
    if warnings:
        lines.extend(["", "AVISOS", *[f"- {warning}" for warning in warnings]])
    write_text(mod_root / "HISTORIA_DO_MUNDO_PT-BR.txt", "\n".join(lines) + "\n")


def validate_world_history(
    states: Dict[str, object],
    plan: StrategicRegionPlan,
    centers: Dict[str, str],
    mod_root: Path,
    options: Dict[str, str],
) -> List[str]:
    messages: List[str] = []
    all_states = set(states)
    flattened = [state for members in plan.assignments.values() for state in members]
    if set(flattened) != all_states or len(flattened) != len(all_states):
        raise ValueError("Regiões estratégicas não cobrem cada estado exatamente uma vez.")
    if any(not members for members in plan.assignments.values()):
        raise ValueError("Há região estratégica vazia.")
    province_to_state = {province.upper(): state for state, values in plan.state_provinces.items() for province in values}
    bad_capitals = [
        region for region, province in plan.capitals.items()
        if province_to_state.get(province.upper()) not in set(plan.assignments[region])
    ]
    if plan.mode != "keep" and bad_capitals:
        raise ValueError(f"Capitais fora de suas regiões estratégicas: {bad_capitals[:5]}")
    # Procedural modes are validated against the exact province raster. Vanilla
    # regions are trusted as shipped and do not require rebuilding the graph.
    if plan.mode != "keep":
        disconnected: List[str] = []
        for region, members in plan.assignments.items():
            allowed = set(members)
            start = members[0]
            seen = {start}
            stack = [start]
            while stack:
                current = stack.pop()
                for neighbour in plan.adjacency[current]:
                    if neighbour in allowed and neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
            if seen != allowed:
                disconnected.append(region)
        if disconnected:
            raise ValueError(f"Regiões estratégicas desconectadas na malha geográfica: {disconnected[:5]}")
    messages.append(
        f"Regiões estratégicas: {len(plan.assignments)} regiões, {len(flattened)} estados, tamanhos {min(map(len, plan.assignments.values()))}–{max(map(len, plan.assignments.values()))}"
    )
    if centers:
        counts = Counter(centers.values())
        if len(centers) != len(set(centers)):
            raise ValueError("Um estado recebeu mais de um centro de civilização.")
        messages.append(f"Centros de civilização: {len(centers)} centros, distribuição {dict(sorted(counts.items()))}")
    history_script = mod_root / "common/scripted_effects/99_bwg_world_history.txt"
    if not history_script.exists() or brace_balance(read_text(history_script)) != 0:
        raise ValueError("Script de história procedural ausente ou inválido.")
    needs_label = options.get("strategic_needs", "natural")
    diplomacy_label = options.get("diplomacy", "natural_relations")
    company_label = options.get("companies", "natural_dynamic")
    overseas_label = options.get("overseas_territories", "rare_colonial")
    subjects_label = options.get("subjects", "very_rare")
    colors_label = options.get("country_colors", "neighbour_contrast")
    country_scale_label = options.get("country_scale", "balanced")
    messages.append(
        f"História procedural: necessidades={needs_label}, companhias={company_label}, diplomacia={diplomacy_label}, ultramar={overseas_label}, súditos={subjects_label}, cores={colors_label}, escala_países={country_scale_label}; corridas por recursos e crises regionais ausentes"
    )
    return messages
