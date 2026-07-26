from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import sys
import textwrap
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

class GenerationCancelled(RuntimeError):
    """Raised when the user rejects the seed preview before saving."""


# ================================================================
# Clausewitz text helpers
# ================================================================

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, text: str, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text
    if bom:
        data = "\ufeff" + data.lstrip("\ufeff")
    path.write_bytes(data.encode("utf-8"))


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
        if c == '#':
            in_comment = True
        elif c == '"':
            in_string = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"Unmatched brace at {open_idx}")


def top_level_blocks(text: str, name_pattern: str) -> List[Tuple[str, int, int, int, int]]:
    """Return (name, assignment_start, open_brace, close_brace, end) for top-level named blocks."""
    rx = re.compile(rf"(?m)^\s*({name_pattern})\s*=\s*\{{")
    results = []
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    candidates = {m.start(): m for m in rx.finditer(text)}
    i = 0
    line_start = True
    while i < len(text):
        if i in candidates and depth == 0 and not in_string and not in_comment:
            m = candidates[i]
            open_idx = text.find('{', m.start(), m.end())
            close_idx = matching_brace(text, open_idx)
            results.append((m.group(1), m.start(), open_idx, close_idx, close_idx + 1))
            i = close_idx + 1
            line_start = False
            continue
        c = text[i]
        if in_comment:
            if c == '\n':
                in_comment = False
        elif in_string:
            if escaped:
                escaped = False
            elif c == '\\':
                escaped = True
            elif c == '"':
                in_string = False
        else:
            if c == '#':
                in_comment = True
            elif c == '"':
                in_string = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
        i += 1
    return results


def find_top_level_fields(block: str, key: str) -> List[Tuple[int, int]]:
    """Find spans of key assignments/blocks directly inside a named block."""
    open0 = block.find('{')
    close0 = matching_brace(block, open0)
    rx = re.compile(rf"(?m)^[ \t]*{re.escape(key)}\s*=\s*")
    spans = []
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    candidates = {m.start(): m for m in rx.finditer(block)}
    i = open0 + 1
    depth = 1
    while i < close0:
        if i in candidates and depth == 1 and not in_string and not in_comment:
            m = candidates[i]
            val_start = m.end()
            while val_start < len(block) and block[val_start].isspace() and block[val_start] not in '\r\n':
                val_start += 1
            if val_start < len(block) and block[val_start] == '{':
                end = matching_brace(block, val_start) + 1
            else:
                nl = block.find('\n', val_start)
                end = len(block) if nl == -1 else nl
            spans.append((m.start(), end))
            i = end
            continue
        c = block[i]
        if in_comment:
            if c == '\n': in_comment = False
        elif in_string:
            if escaped: escaped = False
            elif c == '\\': escaped = True
            elif c == '"': in_string = False
        else:
            if c == '#': in_comment = True
            elif c == '"': in_string = True
            elif c == '{': depth += 1
            elif c == '}': depth -= 1
        i += 1
    return spans


def replace_fields(block: str, replacements: Dict[str, Optional[str]], repeated: Optional[Dict[str, List[str]]] = None) -> str:
    """Replace direct fields. replacement None removes. Missing non-None fields are inserted before closing brace."""
    ops: List[Tuple[int, int, str]] = []
    missing: List[str] = []
    for key, replacement in replacements.items():
        spans = find_top_level_fields(block, key)
        if spans:
            for idx, (a, b) in enumerate(spans):
                ops.append((a, b, replacement if idx == 0 and replacement is not None else ""))
        elif replacement is not None:
            missing.append(replacement)
    if repeated:
        for key, values in repeated.items():
            spans = find_top_level_fields(block, key)
            for a, b in spans:
                ops.append((a, b, ""))
            missing.extend(values)
    for a, b, repl in sorted(ops, reverse=True):
        block = block[:a] + repl + block[b:]
    if missing:
        close_idx = matching_brace(block, block.find('{'))
        insertion = "\n" + "\n".join(missing) + "\n"
        block = block[:close_idx] + insertion + block[close_idx:]
    return block


def brace_balance(text: str) -> int:
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    for c in text:
        if in_comment:
            if c == '\n': in_comment = False
            continue
        if in_string:
            if escaped: escaped = False
            elif c == '\\': escaped = True
            elif c == '"': in_string = False
            continue
        if c == '#': in_comment = True
        elif c == '"': in_string = True
        elif c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth < 0: raise ValueError("Negative brace balance")
    if in_string: raise ValueError("Unclosed string")
    return depth

# ================================================================
# Data structures and parsing
# ================================================================

@dataclass
class ResourceRecord:
    kind: str  # capped or resource
    building: str
    amount: int
    amount_key: str = ""
    discovered_amount: int = 0
    depleted_type: str = ""

    @property
    def total_amount(self) -> int:
        return self.amount + self.discovered_amount

@dataclass
class StateData:
    name: str
    source_file: Path
    macro: str
    province_count: int
    coastal: bool
    arable_land: int
    arable_resources: List[str]
    capped: Dict[str, int]
    resources: List[ResourceRecord]
    traits: List[str] = field(default_factory=list)
    strategic_region: str = "region_unassigned"
    region_order: int = 0
    climate_profile: str = "temperate"
    geology_belts: List[str] = field(default_factory=list)
    population: int = 0
    target_population: int = 0
    new_arable_land: int = 0
    new_arable_resources: List[str] = field(default_factory=list)
    new_capped: Dict[str, int] = field(default_factory=dict)
    new_resources: List[ResourceRecord] = field(default_factory=list)


def parse_state_regions(state_dir: Path) -> Tuple[Dict[str, StateData], Dict[Path, str], Dict[Path, List[Tuple[str,int,int,int,int]]]]:
    states: Dict[str, StateData] = {}
    file_texts: Dict[Path, str] = {}
    file_blocks: Dict[Path, List[Tuple[str,int,int,int,int]]] = {}
    for path in sorted(state_dir.glob("*.txt")):
        if path.name == "state_regions.md" or path.name == "99_seas.txt":
            continue
        text = read_text(path)
        blocks = top_level_blocks(text, r"STATE_[A-Z0-9_]+")
        file_texts[path] = text
        file_blocks[path] = blocks
        for name, start, op, cl, end in blocks:
            block = text[start:end]
            provinces_m = re.search(r"\bprovinces\s*=\s*\{([^{}]*)\}", block, re.S)
            province_count = len(re.findall(r'"x[0-9A-Fa-f]+"', provinces_m.group(1))) if provinces_m else 1
            coastal = bool(re.search(r"(?m)^\s*(port|naval_exit_id)\s*=", block))
            ar_m = re.search(r"(?m)^\s*arable_land\s*=\s*(\d+)", block)
            arable_land = int(ar_m.group(1)) if ar_m else 0
            arr_m = re.search(r"\barable_resources\s*=\s*\{([^{}]*)\}", block, re.S)
            arable_resources = re.findall(r'"(building_[a-z0-9_]+)"', arr_m.group(1)) if arr_m else []
            traits_m = re.search(r"\btraits\s*=\s*\{([^{}]*)\}", block, re.S)
            traits = re.findall(r'"?([a-z0-9_]*state_trait_[a-z0-9_]+)"?', traits_m.group(1)) if traits_m else []
            capped: Dict[str,int] = {}
            cap_m = re.search(r"\bcapped_resources\s*=\s*\{([^{}]*)\}", block, re.S)
            if cap_m:
                for b, n in re.findall(r"\b(building_[a-z0-9_]+)\s*=\s*(\d+)", cap_m.group(1)):
                    capped[b] = int(n)
            resources: List[ResourceRecord] = []
            for a,b in find_top_level_fields(block, "resource"):
                rb = block[a:b]
                tm = re.search(r"\btype\s*=\s*\"?([a-z0-9_]+)\"?", rb)
                und = re.search(r"\bundiscovered_amount\s*=\s*(\d+)", rb)
                disc = re.search(r"\bdiscovered_amount\s*=\s*(\d+)", rb)
                dep = re.search(r"\bdepleted_type\s*=\s*\"?([a-z0-9_]+)\"?", rb)
                if tm and (und or disc):
                    if und:
                        amount=int(und.group(1)); amount_key="undiscovered_amount"; discovered=int(disc.group(1)) if disc else 0
                    else:
                        amount=int(disc.group(1)); amount_key="discovered_amount"; discovered=0
                    resources.append(ResourceRecord("resource", tm.group(1), amount, amount_key, discovered, dep.group(1) if dep else ""))
            states[name] = StateData(
                name=name, source_file=path, macro=path.stem,
                province_count=province_count, coastal=coastal,
                arable_land=arable_land, arable_resources=arable_resources,
                capped=capped, resources=resources, traits=traits,
                new_arable_land=arable_land,
                new_arable_resources=list(arable_resources),
                new_capped=dict(capped),
                new_resources=list(resources),
            )
    return states, file_texts, file_blocks


def parse_strategic_regions(region_dir: Path) -> Dict[str, str]:
    mapping: Dict[str,str] = {}
    for path in sorted(region_dir.glob("*.txt")):
        if path.name.endswith(".md") or "water" in path.name:
            continue
        text = read_text(path)
        for region, start, op, cl, end in top_level_blocks(text, r"region_[a-z0-9_]+"):
            block = text[start:end]
            sm = re.search(r"\bstates\s*=\s*\{([^{}]*)\}", block, re.S)
            if not sm: continue
            for state in re.findall(r"\bSTATE_[A-Z0-9_]+\b", sm.group(1)):
                mapping.setdefault(state, region)
    return mapping


def parse_strategic_region_order(region_dir: Path) -> Tuple[Dict[str, List[str]], List[str]]:
    ordered: Dict[str, List[str]] = {}
    region_sequence: List[str] = []
    for path in sorted(region_dir.glob("*.txt")):
        if path.name.endswith(".md") or "water" in path.name:
            continue
        text = read_text(path)
        for region, start, op, cl, end in top_level_blocks(text, r"region_[a-z0-9_]+"):
            block = text[start:end]
            sm = re.search(r"\bstates\s*=\s*\{([^{}]*)\}", block, re.S)
            if not sm:
                continue
            states = re.findall(r"\bSTATE_[A-Z0-9_]+\b", sm.group(1))
            if states:
                ordered[region] = states
                region_sequence.append(region)
    return ordered, region_sequence


def classify_climate(state: StateData) -> str:
    trait_text = " ".join(state.traits).lower()
    macro = state.macro
    if any(k in trait_text for k in ("desert", "sahara", "arid", "outback")):
        return "arid"
    if any(k in trait_text for k in ("arctic", "tundra", "permafrost")):
        return "cold"
    if macro.startswith(("14_",)):
        return "cold"
    if macro.startswith(("03_", "08_", "09_")):
        return "arid"
    if macro.startswith(("04_", "06_", "07_", "10_", "12_", "13_")):
        return "tropical"
    if macro.startswith("11_"):
        return "subtropical"
    return "temperate"

@dataclass
class PopEntry:
    culture: str
    size: int
    religion: str = ""
    pop_type: str = ""


@dataclass
class PopStateBlock:
    state: str
    path: Path
    full_text: str
    block_start: int
    block_end: int
    size_spans: List[Tuple[int,int,int]]  # absolute start/end/value
    total: int
    entries: List[PopEntry] = field(default_factory=list)
    wrapper_keys: List[str] = field(default_factory=list)
    wrapper_totals: List[int] = field(default_factory=list)
    assigned_entries: List[PopEntry] = field(default_factory=list)

    @property
    def dominant_culture(self) -> str:
        by_culture = Counter()
        for e in self.entries:
            by_culture[e.culture] += e.size
        return by_culture.most_common(1)[0][0] if by_culture else ""


def _parse_pop_entries(text: str) -> List[PopEntry]:
    entries: List[PopEntry] = []
    for m in re.finditer(r"create_pop\s*=\s*\{", text):
        op = text.find('{', m.start(), m.end())
        cl = matching_brace(text, op)
        block = text[op + 1:cl]
        cm = re.search(r"\bculture\s*=\s*([a-zA-Z0-9_]+)", block)
        sm = re.search(r"\bsize\s*=\s*(\d+)", block)
        if not cm or not sm:
            continue
        rm = re.search(r"\breligion\s*=\s*([a-zA-Z0-9_]+)", block)
        pm = re.search(r"\bpop_type\s*=\s*([a-zA-Z0-9_]+)", block)
        entries.append(PopEntry(
            culture=cm.group(1),
            size=int(sm.group(1)),
            religion=rm.group(1) if rm else "",
            pop_type=pm.group(1) if pm else "",
        ))
    return entries


def parse_population(pop_dir: Path) -> Tuple[Dict[str, PopStateBlock], Dict[Path,str]]:
    result: Dict[str,PopStateBlock] = {}
    texts: Dict[Path,str] = {}
    for path in sorted(pop_dir.glob("*.txt")):
        text = read_text(path)
        texts[path] = text
        outer_blocks = top_level_blocks(text, r"POPS")
        if not outer_blocks:
            continue
        _, _, op, cl, _ = outer_blocks[0]
        inner = text[op+1:cl]
        offset = op+1
        for name, start, bop, bcl, end in top_level_blocks(inner, r"s:STATE_[A-Z0-9_]+"):
            state = name[2:]
            abs_start, abs_end = offset+start, offset+end
            block = text[abs_start:abs_end]
            spans=[]
            total=0
            for m in re.finditer(r"(?m)^([ \t]*size\s*=\s*)(\d+)", block):
                val=int(m.group(2)); total += val
                spans.append((abs_start+m.start(2), abs_start+m.end(2), val))
            entries = _parse_pop_entries(block)
            wrapper_keys: List[str] = []
            wrapper_totals: List[int] = []
            state_open=block.find('{')
            state_close=matching_brace(block,state_open)
            state_inner=block[state_open+1:state_close]
            wrappers = top_level_blocks(state_inner, r"region_state:[A-Z0-9_]+")
            for wrapper, ws, wop, wcl, we in wrappers:
                wrapper_keys.append(wrapper)
                wrapper_entries = _parse_pop_entries(state_inner[ws:we])
                wrapper_totals.append(sum(e.size for e in wrapper_entries))
            if not wrapper_keys:
                # Defensive fallback for modded history formats.
                wrapper_keys = ["region_state:ROOT"]
                wrapper_totals = [total]
            result[state] = PopStateBlock(
                state,path,text,abs_start,abs_end,spans,total,
                entries=entries,wrapper_keys=wrapper_keys,wrapper_totals=wrapper_totals,
                assigned_entries=[PopEntry(e.culture,e.size,e.religion,e.pop_type) for e in entries],
            )
    return result,texts

# ================================================================
# Allocation helpers
# ================================================================

def largest_remainder(total: int, weights: Sequence[float], minimums: Optional[Sequence[int]]=None, caps: Optional[Sequence[int]]=None) -> List[int]:
    n=len(weights)
    if n==0: return []
    mins=list(minimums or [0]*n)
    maxs=list(caps or [10**18]*n)
    if sum(mins)>total:
        # proportional minimum reduction
        mins=[0]*n
    out=mins[:]
    remaining=total-sum(out)
    active=[i for i in range(n) if out[i]<maxs[i]]
    while remaining>0 and active:
        sw=sum(max(0.0,weights[i]) for i in active)
        if sw<=0: sw=float(len(active)); wvals={i:1.0 for i in active}
        else: wvals={i:max(0.0,weights[i]) for i in active}
        raw={i:remaining*wvals[i]/sw for i in active}
        floors={i:min(maxs[i]-out[i], int(math.floor(raw[i]))) for i in active}
        used=sum(floors.values())
        for i,v in floors.items(): out[i]+=v
        remaining-=used
        if remaining<=0: break
        ranked=sorted(active,key=lambda i:(raw[i]-math.floor(raw[i]), -i),reverse=True)
        progressed=False
        for i in ranked:
            if remaining<=0: break
            if out[i]<maxs[i]:
                out[i]+=1; remaining-=1; progressed=True
        active=[i for i in active if out[i]<maxs[i]]
        if not progressed and used==0: break
    return out


def weighted_sample_without_replacement(items: Sequence[str], weights: Sequence[float], k: int, rng: random.Random) -> List[str]:
    pool=list(items); w=list(weights); chosen=[]
    for _ in range(min(k,len(pool))):
        total=sum(max(0.0,x) for x in w)
        if total<=0:
            idx=rng.randrange(len(pool))
        else:
            r=rng.random()*total; acc=0; idx=len(pool)-1
            for j,x in enumerate(w):
                acc += max(0.0,x)
                if r<=acc:
                    idx=j; break
        chosen.append(pool.pop(idx)); w.pop(idx)
    return chosen


def sigma_for_intensity(intensity: str) -> float:
    return {"low":0.14,"medium":0.32,"high":0.60}.get(intensity,0.32)


def group_states(states: Dict[str,StateData], mode: str) -> Dict[str,List[StateData]]:
    groups=defaultdict(list)
    for s in states.values():
        if mode=="global": key="world"
        elif mode=="macro": key=s.macro
        else: key=s.strategic_region
        groups[key].append(s)
    return groups

# ================================================================
# Randomization algorithms
# ================================================================

STAPLES={"building_rye_farm","building_wheat_farm","building_rice_farm","building_maize_farm","building_millet_farm"}
LIVESTOCK="building_livestock_ranch"


def randomize_arable_land(states: Dict[str,StateData], rng: random.Random, scope: str, intensity: str) -> None:
    """Redistribute arable land. Global mode intentionally forgets the vanilla regional total."""
    sigma=sigma_for_intensity(intensity)
    mode={"regional":"strategic","global":"global","macro":"macro"}.get(scope,"global")
    for _, group in group_states(states,mode).items():
        total=sum(s.arable_land for s in group)
        if total<=0: continue
        weights=[]; mins=[]; caps=[]
        avg=total/max(1,len(group))
        for s in group:
            geography=max(1,s.province_count)**0.78
            # Regional mode keeps only a light memory of local farming; global mode has none.
            memory=(max(1,s.arable_land)**0.10) if mode!="global" else 1.0
            noise=math.exp(rng.gauss(0,sigma*0.75))
            weights.append(geography*memory*noise)
            mins.append(1)
            caps.append(max(12,int(avg*4.5),int(6+2.4*math.sqrt(max(1,s.province_count)))))
        # Expand caps only if necessary, while keeping a hard anti-megastate bias.
        while sum(caps)<total:
            caps=[c+max(1,int(avg*0.35)) for c in caps]
        alloc=largest_remainder(total,weights,mins,caps)
        for s,v in zip(group,alloc): s.new_arable_land=v

CLIMATE_CROPS: Dict[str, set[str]] = {
    "cold": {
        "building_rye_farm", "building_wheat_farm", "building_livestock_ranch",
    },
    "temperate": {
        "building_rye_farm", "building_wheat_farm", "building_maize_farm",
        "building_livestock_ranch", "building_vineyard", "building_tobacco_plantation",
        "building_sugar_plantation", "building_cotton_plantation",
    },
    "subtropical": {
        "building_wheat_farm", "building_maize_farm", "building_rice_farm",
        "building_millet_farm", "building_livestock_ranch", "building_vineyard",
        "building_cotton_plantation", "building_sugar_plantation",
        "building_tobacco_plantation", "building_tea_plantation",
        "building_silk_plantation", "building_dye_plantation",
    },
    "tropical": {
        "building_rice_farm", "building_maize_farm", "building_millet_farm",
        "building_livestock_ranch", "building_banana_plantation",
        "building_coffee_plantation", "building_cotton_plantation",
        "building_sugar_plantation", "building_tobacco_plantation",
        "building_tea_plantation", "building_dye_plantation",
        "building_opium_plantation", "building_silk_plantation",
    },
    "arid": {
        "building_wheat_farm", "building_millet_farm", "building_livestock_ranch",
        "building_cotton_plantation", "building_opium_plantation",
        "building_tobacco_plantation", "building_dye_plantation",
    },
}

CROP_CLIMATE_PREFERENCE: Dict[str, tuple[str, ...]] = {
    "building_rye_farm": ("cold", "temperate"),
    "building_wheat_farm": ("temperate", "arid", "subtropical", "cold"),
    "building_rice_farm": ("tropical", "subtropical"),
    "building_maize_farm": ("tropical", "subtropical", "temperate"),
    "building_millet_farm": ("arid", "tropical", "subtropical"),
    "building_livestock_ranch": ("cold", "temperate", "arid", "subtropical", "tropical"),
    "building_vineyard": ("temperate", "subtropical"),
    "building_cotton_plantation": ("subtropical", "arid", "tropical", "temperate"),
    "building_sugar_plantation": ("tropical", "subtropical", "temperate"),
    "building_tobacco_plantation": ("subtropical", "tropical", "temperate", "arid"),
    "building_coffee_plantation": ("tropical", "subtropical"),
    "building_tea_plantation": ("subtropical", "tropical"),
    "building_dye_plantation": ("tropical", "subtropical", "arid"),
    "building_opium_plantation": ("arid", "subtropical", "tropical"),
    "building_banana_plantation": ("tropical", "subtropical"),
    "building_silk_plantation": ("subtropical", "tropical"),
}


def _crop_is_compatible(state: StateData, crop: str, chaos: bool) -> bool:
    if chaos:
        return True
    return crop in CLIMATE_CROPS.get(state.climate_profile, CLIMATE_CROPS["temperate"])


def randomize_arable_resources(states: Dict[str,StateData], rng: random.Random, scope: str, intensity: str) -> None:
    """Create climate-coherent agricultural zones while preserving worldwide availability counts."""
    sigma=sigma_for_intensity(intensity)
    chaos = scope in {"global_chaos", "chaos"}
    regional = scope in {"regional", "regional_natural"}
    mode = "strategic" if regional else "global"

    for _, group in group_states(states, mode).items():
        arable_states=[s for s in group if s.new_arable_land>0]
        if not arable_states:
            continue
        original_counts=Counter(r for s in group for r in s.arable_resources)
        for s in arable_states:
            s.new_arable_resources=[]
        load=Counter({s.name:0 for s in arable_states})

        ordered=sorted(
            original_counts,
            key=lambda r:(0 if r in STAPLES else 1 if r==LIVESTOCK else 2, -original_counts[r], r),
        )
        for crop in ordered:
            k=min(original_counts[crop], len(arable_states))
            eligible=[s for s in arable_states if _crop_is_compatible(s,crop,chaos)]
            if len(eligible)<k:
                # Preserve exact counts even when source totals exceed strict climatic capacity.
                fallback=[s for s in arable_states if s not in eligible]
                fallback.sort(key=lambda s:(
                    CROP_CLIMATE_PREFERENCE.get(crop,()).index(s.climate_profile)
                    if s.climate_profile in CROP_CLIMATE_PREFERENCE.get(crop,()) else 99,
                    load[s.name], -s.new_arable_land,
                ))
                eligible += fallback[:k-len(eligible)]
            names=[s.name for s in eligible]
            weights=[]
            for s in eligible:
                compatible_bonus=2.2 if _crop_is_compatible(s,crop,chaos) else 0.25
                staple_bonus=1.35 if crop in STAPLES and not any(x in STAPLES for x in s.new_arable_resources) else 1.0
                diversity_penalty=(1+load[s.name])**(2.6 if crop in STAPLES else 2.0)
                regional_noise=math.exp(rng.gauss(0,sigma*0.35))
                weights.append(
                    compatible_bonus*staple_bonus*(max(1,s.new_arable_land)**0.60)*regional_noise/diversity_penalty
                )
            chosen=weighted_sample_without_replacement(names,weights,k,rng)
            for name in chosen:
                states[name].new_arable_resources.append(crop)
                load[name]+=1

        # Guarantee every arable state has a food-producing option without changing the total count:
        # move a staple/livestock option from overloaded donors.
        empty=[s for s in arable_states if not s.new_arable_resources]
        for receiver in empty:
            donors=sorted(
                (s for s in arable_states if len(s.new_arable_resources)>1),
                key=lambda s:(len(s.new_arable_resources),s.new_arable_land), reverse=True,
            )
            moved=False
            for donor in donors:
                candidates=sorted(
                    donor.new_arable_resources,
                    key=lambda crop:(
                        crop not in STAPLES and crop!=LIVESTOCK,
                        not _crop_is_compatible(receiver,crop,chaos),
                    ),
                )
                for crop in candidates:
                    if crop not in receiver.new_arable_resources:
                        donor.new_arable_resources.remove(crop)
                        receiver.new_arable_resources.append(crop)
                        moved=True
                        break
                if moved:
                    break
            if not moved:
                receiver.new_arable_resources.append(LIVESTOCK)

        # Keep crop menus readable: cap ordinary states at five options by moving surplus to low-load states.
        for donor in sorted(arable_states,key=lambda s:len(s.new_arable_resources),reverse=True):
            while len(donor.new_arable_resources)>5:
                crop=donor.new_arable_resources[-1]
                candidates=[s for s in arable_states if crop not in s.new_arable_resources and len(s.new_arable_resources)<5]
                if not candidates:
                    break
                candidates.sort(key=lambda s:(not _crop_is_compatible(s,crop,chaos),len(s.new_arable_resources),-s.new_arable_land))
                receiver=candidates[0]
                donor.new_arable_resources.remove(crop)
                receiver.new_arable_resources.append(crop)

        for s in arable_states:
            s.new_arable_resources=list(dict.fromkeys(s.new_arable_resources))


def resource_eligible(s: StateData, building: str, plausibility: str) -> bool:
    if building in {"building_fishing_wharf","building_whaling_station"}:
        return s.coastal
    if plausibility=="chaos":
        return True
    if building=="building_rubber_plantation":
        # Warm/tropical macro-regions only in plausible mode.
        warm_prefixes=("04_","06_","07_","10_","11_","12_","13_")
        return s.macro.startswith(warm_prefixes)
    return True

def allocate_region_quotas(eligible_by_region: Dict[str,List[StateData]], count: int, rng: random.Random, original_by_region: Counter, plausibility: str) -> Dict[str,int]:
    """Allocate deposits worldwide without using the old resource map as a weight."""
    regions=[r for r,v in eligible_by_region.items() if v]
    if not regions: return {}
    capacities=[]
    for r in regions:
        pool=eligible_by_region[r]
        capacities.append(sum(max(1,s.province_count)**0.70 for s in pool))
    total_capacity=sum(capacities) or 1.0
    # Half equal regional opportunity, half physical capacity. This prevents Europe from
    # inheriting its vanilla advantage merely because its old deposits were numerous.
    weights=[]; caps=[]
    for r,cap in zip(regions,capacities):
        equal=1/len(regions)
        physical=cap/total_capacity
        weights.append((0.70*equal+0.30*physical)*math.exp(rng.gauss(0,0.08)))
        caps.append(len(eligible_by_region[r]))
    mins=[1 if count>=len(regions) else 0 for _ in regions]
    alloc=largest_remainder(count,weights,mins,caps)
    return dict(zip(regions,alloc))

RESOURCE_AMOUNT_CAPS = {
    # Smaller total deposits than v3: reserves are spread across more states.
    "building_logging_camp": 18,
    "building_fishing_wharf": 8,
    "building_whaling_station": 4,
    "building_coal_mine": 32,
    "building_iron_mine": 30,
    "building_lead_mine": 18,
    "building_sulfur_mine": 18,
    "building_gold_mine": 4,
    "building_gold_field": 8,
    "building_oil_rig": 28,
    "building_rubber_plantation": 20,
}
STRATEGIC_RESOURCE_BUILDINGS = {
    "building_coal_mine","building_iron_mine","building_lead_mine","building_sulfur_mine",
    "building_gold_mine","building_gold_field","building_oil_rig","building_rubber_plantation",
}

# These resources normally use capped_resources. In gradual mode they are converted
# to resource blocks, and bg_mining is made discoverable so the hidden portion can
# be revealed by the native discovery system.
GRADUAL_MINERAL_BUILDINGS = {
    "building_coal_mine","building_iron_mine","building_lead_mine",
    "building_sulfur_mine","building_gold_mine",
}
NATIVE_DISCOVERABLE_BUILDINGS = {
    "building_gold_field","building_oil_rig","building_rubber_plantation",
}

VISIBLE_FRACTION_PROFILES = {
    "very_sparse": {
        "building_coal_mine": 0.12, "building_iron_mine": 0.15,
        "building_lead_mine": 0.08, "building_sulfur_mine": 0.08,
        "building_gold_mine": 0.02, "building_gold_field": 0.02,
        "building_oil_rig": 0.0, "building_rubber_plantation": 0.0,
    },
    "sparse": {
        "building_coal_mine": 0.22, "building_iron_mine": 0.25,
        "building_lead_mine": 0.16, "building_sulfur_mine": 0.16,
        "building_gold_mine": 0.05, "building_gold_field": 0.05,
        "building_oil_rig": 0.0, "building_rubber_plantation": 0.0,
    },
    "moderate": {
        "building_coal_mine": 0.35, "building_iron_mine": 0.40,
        "building_lead_mine": 0.30, "building_sulfur_mine": 0.30,
        "building_gold_mine": 0.10, "building_gold_field": 0.10,
        "building_oil_rig": 0.0, "building_rubber_plantation": 0.0,
    },
}
VISIBLE_PER_DEPOSIT_CAPS = {
    "building_coal_mine": 5, "building_iron_mine": 5,
    "building_lead_mine": 3, "building_sulfur_mine": 3,
    "building_gold_mine": 1, "building_gold_field": 1,
    "building_oil_rig": 0, "building_rubber_plantation": 0,
}


def resource_mode_parts(mode: str) -> Tuple[str, bool]:
    """Return (geographic plausibility, gradual discovery enabled)."""
    if mode in {"plausible_gradual", "chaos_gradual"}:
        return mode.removesuffix("_gradual"), True
    if mode in {"plausible_full", "chaos_full"}:
        return mode.removesuffix("_full"), False
    # Backward compatibility with v3 option values.
    return mode, False


def allocate_visible_amounts(
    building: str,
    amounts: Sequence[int],
    rng: random.Random,
    profile: str,
) -> List[int]:
    fractions=VISIBLE_FRACTION_PROFILES.get(profile,VISIBLE_FRACTION_PROFILES["sparse"])
    fraction=fractions.get(building,1.0)
    total=sum(amounts)
    target=max(0,min(total,int(round(total*fraction))))
    if target<=0:
        return [0]*len(amounts)
    per_cap=VISIBLE_PER_DEPOSIT_CAPS.get(building,max(amounts,default=0))
    caps=[min(a,per_cap) for a in amounts]
    target=min(target,sum(caps))
    weights=[(a**0.55)*(0.75+rng.random()*0.5) for a in amounts]
    visible=largest_remainder(target,weights,[0]*len(amounts),caps)
    # Spread at least one visible level to as many deposits as the target permits.
    zero_candidates=[i for i,(a,v) in enumerate(zip(amounts,visible)) if a>0 and v==0 and caps[i]>0]
    donors=[i for i,v in enumerate(visible) if v>1]
    rng.shuffle(zero_candidates); rng.shuffle(donors)
    for receiver in zero_candidates:
        if not donors: break
        donor=donors[-1]
        visible[donor]-=1; visible[receiver]+=1
        if visible[donor]<=1: donors.pop()
    return visible


BELT_REGION_TARGETS = {
    "building_coal_mine": (8, 12),
    "building_iron_mine": (8, 12),
    "building_lead_mine": (6, 9),
    "building_sulfur_mine": (6, 9),
    "building_gold_mine": (4, 7),
    "building_gold_field": (4, 7),
    "building_oil_rig": (5, 8),
    "building_rubber_plantation": (6, 10),
}


def _contiguous_state_pick(pool: List[StateData], k: int, rng: random.Random, load: Counter, strategic_load: Counter, building: str) -> List[StateData]:
    if k <= 0 or not pool:
        return []
    ordered=sorted(pool,key=lambda s:(s.region_order,s.name))
    if k>=len(ordered):
        return ordered[:]
    # Pick several short neighbouring runs rather than isolated states.
    chosen: List[StateData] = []
    used=set()
    remaining=k
    while remaining>0:
        run=min(remaining, rng.randint(2, min(5,remaining)) if remaining>=2 else 1)
        candidate_starts=[]
        weights=[]
        for i in range(0,len(ordered)-run+1):
            segment=ordered[i:i+run]
            if any(s.name in used for s in segment):
                continue
            penalty=1.0
            for s in segment:
                penalty /= (1+load[s.name])**1.25
                if building in STRATEGIC_RESOURCE_BUILDINGS:
                    penalty /= (1+strategic_load[s.name])**1.8
            candidate_starts.append(i)
            weights.append(max(0.0001,penalty))
        if not candidate_starts:
            break
        start_idx=weighted_sample_without_replacement([str(x) for x in candidate_starts],weights,1,rng)[0]
        segment=ordered[int(start_idx):int(start_idx)+run]
        for s in segment:
            chosen.append(s); used.add(s.name)
        remaining-=len(segment)
    if remaining>0:
        leftovers=[s for s in ordered if s.name not in used]
        leftovers.sort(key=lambda s:(strategic_load[s.name],load[s.name],rng.random()))
        chosen.extend(leftovers[:remaining])
    return chosen[:k]


def randomize_resources(
    states: Dict[str,StateData],
    rng: random.Random,
    mode: str,
    intensity: str,
    visibility_profile: str = "sparse",
) -> None:
    """Rebuild exact world resource totals as regional geological belts and renewable zones."""
    plausibility, gradual = resource_mode_parts(mode)
    records_by_key: Dict[Tuple[str,str],List[Tuple[str,ResourceRecord]]]=defaultdict(list)
    for s in states.values():
        for b,n in s.capped.items():
            records_by_key[("capped",b)].append((s.name,ResourceRecord("capped",b,n)))
        for r in s.resources:
            records_by_key[("resource",r.building)].append((s.name,r))
    for s in states.values():
        s.new_capped={}; s.new_resources=[]; s.geology_belts=[]

    strategic_load=Counter()
    total_load=Counter()
    region_belt_load=Counter()
    sigma=sigma_for_intensity(intensity)

    for (kind,building), records in sorted(records_by_key.items(),key=lambda kv:(kv[0][0],kv[0][1])):
        total=sum(r.total_amount for _,r in records)
        if total<=0:
            continue
        hard_cap=RESOURCE_AMOUNT_CAPS.get(building,max(8,int(math.ceil(total/max(1,len(records))))))
        eligible=[s for s in states.values() if resource_eligible(s,building,plausibility)]
        if not eligible:
            continue
        deposit_count=max(1,int(math.ceil((total/hard_cap)*1.08)))
        deposit_count=min(deposit_count,len(eligible))
        eligible_by_region=defaultdict(list)
        for s in eligible:
            eligible_by_region[s.strategic_region].append(s)

        selected: List[StateData] = []
        # Renewable resources are broadly available; minerals form geographic belts.
        if building not in STRATEGIC_RESOURCE_BUILDINGS:
            quotas=allocate_region_quotas(eligible_by_region,deposit_count,rng,Counter(),plausibility)
            for region,k in quotas.items():
                pool=eligible_by_region[region]
                names=[s.name for s in pool]
                weights=[]
                for s in pool:
                    climate_bonus=1.0
                    if building=="building_logging_camp":
                        climate_bonus={"tropical":1.35,"temperate":1.30,"subtropical":1.15,"cold":0.75,"arid":0.18}.get(s.climate_profile,1.0)
                    weights.append(climate_bonus*max(1,s.province_count)**0.62/((1+total_load[s.name])**1.4))
                for name in weighted_sample_without_replacement(names,weights,min(k,len(pool)),rng):
                    selected.append(states[name])
        else:
            regions=[r for r,pool in eligible_by_region.items() if pool]
            lo,hi=BELT_REGION_TARGETS.get(building,(5,9))
            belt_count=min(len(regions),deposit_count,max(1,rng.randint(min(lo,len(regions)),min(hi,len(regions))) if regions else 1))
            region_weights=[]
            for region in regions:
                pool=eligible_by_region[region]
                capacity=sum(max(1,s.province_count)**0.65 for s in pool)
                climate_bonus=1.0
                if building=="building_rubber_plantation":
                    tropical_share=sum(s.climate_profile in {"tropical","subtropical"} for s in pool)/max(1,len(pool))
                    climate_bonus=0.15+2.2*tropical_share
                region_weights.append(capacity**0.35*climate_bonus/((1+region_belt_load[region])**1.7))
            belt_regions=weighted_sample_without_replacement(regions,region_weights,belt_count,rng)
            belt_weights=[]
            belt_caps=[]
            for region in belt_regions:
                pool=eligible_by_region[region]
                belt_weights.append((len(pool)**0.30)*math.exp(rng.gauss(0,sigma*0.25)))
                belt_caps.append(len(pool))
            quotas=largest_remainder(deposit_count,belt_weights,[1]*len(belt_regions),belt_caps)
            for region,k in zip(belt_regions,quotas):
                pool=[s for s in eligible_by_region[region] if total_load[s.name]<6 and strategic_load[s.name]<3]
                part=_contiguous_state_pick(pool,min(k,len(pool)),rng,total_load,strategic_load,building)
                for s in part:
                    if s not in selected:
                        selected.append(s)
                region_belt_load[region]+=1

        if len(selected)<deposit_count:
            remaining=[s for s in eligible if s not in selected and strategic_load[s.name]<3 and total_load[s.name]<6]
            remaining.sort(key=lambda s:(strategic_load[s.name],total_load[s.name],region_belt_load[s.strategic_region],rng.random()))
            selected.extend(remaining[:deposit_count-len(selected)])
        if len(selected)<deposit_count:
            # Only malformed or extremely restrictive custom maps should reach this fallback.
            remaining=[s for s in eligible if s not in selected]
            remaining.sort(key=lambda s:(strategic_load[s.name],total_load[s.name],rng.random()))
            selected.extend(remaining[:deposit_count-len(selected)])
        selected=selected[:deposit_count]

        amount_weights=[]
        for s in selected:
            center_bonus=1.0+0.16*sum(1 for x in selected if x.strategic_region==s.strategic_region and abs(x.region_order-s.region_order)<=1)
            amount_weights.append(max(1,s.province_count)**0.48*center_bonus*math.exp(rng.gauss(0,sigma*0.20)))
        amounts=largest_remainder(total,amount_weights,[1]*len(selected),[hard_cap]*len(selected))
        if sum(amounts)!=total:
            raise ValueError(f"Could not allocate exact total for {building}: {sum(amounts)} != {total}")

        depleted_type=""
        for _,r in records:
            depleted_type=depleted_type or r.depleted_type

        if gradual and building in (GRADUAL_MINERAL_BUILDINGS | NATIVE_DISCOVERABLE_BUILDINGS):
            discovered=allocate_visible_amounts(building,amounts,rng,visibility_profile)
        else:
            discovered_total=0
            for _,r in records:
                if r.amount_key=="discovered_amount":
                    discovered_total += r.amount
                discovered_total += r.discovered_amount
            if building in {"building_oil_rig","building_rubber_plantation"}:
                discovered_total=0
            discovered=largest_remainder(discovered_total,[rng.random()+0.1 for _ in selected],[0]*len(selected),amounts) if discovered_total else [0]*len(selected)

        for s,amount,disc in zip(selected,amounts,discovered):
            total_load[s.name]+=1
            if building in STRATEGIC_RESOURCE_BUILDINGS:
                strategic_load[s.name]+=1
                s.geology_belts.append(building)
            if gradual and building in GRADUAL_MINERAL_BUILDINGS:
                s.new_resources.append(ResourceRecord("resource",building,amount-disc,"undiscovered_amount",disc,depleted_type))
            elif kind=="capped":
                s.new_capped[building]=amount
            else:
                s.new_resources.append(ResourceRecord("resource",building,amount-disc,"undiscovered_amount",disc,depleted_type))

def resource_score(s: StateData) -> float:
    weights={
        "building_logging_camp":0.9,"building_fishing_wharf":0.55,"building_whaling_station":0.45,
        "building_coal_mine":1.1,"building_iron_mine":1.0,"building_lead_mine":0.8,"building_sulfur_mine":0.85,
        "building_gold_mine":1.4,
    }
    score=sum(n*weights.get(b,0.7) for b,n in s.new_capped.items())
    for r in s.new_resources:
        score += r.total_amount*({"building_oil_rig":1.35,"building_rubber_plantation":1.05,"building_gold_field":1.5}.get(r.building,0.9))
    return score


def visible_resource_score(s: StateData) -> float:
    score=0.0
    for b,n in s.new_capped.items():
        score += n*{
            "building_logging_camp":1.0,"building_fishing_wharf":1.15,"building_whaling_station":0.45,
            "building_coal_mine":0.55,"building_iron_mine":0.55,"building_lead_mine":0.40,"building_sulfur_mine":0.40,
        }.get(b,0.35)
    for r in s.new_resources:
        visible=r.discovered_amount if r.amount_key=="undiscovered_amount" else r.amount
        score += visible*{
            "building_gold_field":0.35,"building_gold_mine":0.45,"building_oil_rig":0.20,"building_rubber_plantation":0.30,
            "building_coal_mine":0.55,"building_iron_mine":0.55,"building_lead_mine":0.40,"building_sulfur_mine":0.40,
        }.get(r.building,0.30)
    return score


def state_carrying_capacity(s: StateData) -> float:
    climate_food={"temperate":1.12,"subtropical":1.10,"tropical":1.03,"arid":0.58,"cold":0.62}.get(s.climate_profile,1.0)
    staple_count=sum(r in STAPLES for r in s.new_arable_resources)
    crop_diversity=min(4,len(s.new_arable_resources))
    arable_component=s.new_arable_land*16000*climate_food*(0.82+0.12*staple_count+0.035*crop_diversity)
    extraction_component=visible_resource_score(s)*5200
    geographic_component=45000+math.sqrt(max(1,s.province_count))*105000
    coastal_component=90000 if s.coastal else 0
    river_soil_bonus=1.0
    trait_text=" ".join(s.traits).lower()
    if any(k in trait_text for k in ("river","good_soils","fertile","delta","valley")):
        river_soil_bonus*=1.16
    if any(k in trait_text for k in ("desert","sahara","tundra","mountain","highlands")):
        river_soil_bonus*=0.84
    return max(25000,(arable_component+extraction_component+geographic_component+coastal_component)*river_soil_bonus)


def _population_hard_cap(s: StateData) -> int:
    """Physical ceiling used by the procedural population allocator.

    Earlier versions allowed a state to take up to 24% of an entire strategic
    region, which could put ten million people in the Azores or another small
    island.  The ceiling is now tied to the state's own carrying capacity and
    province count.  Large dense cores remain possible; tiny islands cannot
    become accidental demographic super-centres.
    """
    physical = state_carrying_capacity(s)
    province_guard = 120000 + max(1, s.province_count) * 175000
    return max(80000, int(min(physical * 1.30, max(physical * 0.90, province_guard * 2.2))))


def _population_floor(s: StateData, cap: int) -> int:
    # Five thousand people per arable level roughly fills the automatic
    # subsistence buildings without forcing a dense industrial population.
    return min(cap, max(1000, int(s.new_arable_land * 5000)))


def randomize_population(states: Dict[str,StateData], pop_blocks: Dict[str,PopStateBlock], rng: random.Random, scope: str, intensity: str) -> None:
    sigma=sigma_for_intensity(intensity)
    for name,pb in pop_blocks.items():
        if name in states:
            states[name].population=pb.total
    active={k:v for k,v in states.items() if k in pop_blocks and pop_blocks[k].total>0}
    if not active:
        return

    if scope=="global":
        world_total=sum(s.population for s in active.values())
        regions=group_states(active,"strategic")
        region_names=sorted(regions)
        state_caps={name:_population_hard_cap(s) for name,s in active.items()}
        region_physical=[sum(state_carrying_capacity(s) for s in regions[r]) for r in region_names]
        total_physical=sum(region_physical) or 1.0
        region_weights=[]
        region_mins=[]
        region_caps=[]
        for region,physical in zip(region_names,region_physical):
            equal=1/len(region_names)
            physical_share=physical/total_physical
            region_weights.append((0.20*equal+0.80*physical_share)*math.exp(rng.gauss(0,sigma*0.10)))
            cap=sum(state_caps[s.name] for s in regions[region])
            floor=sum(_population_floor(s,state_caps[s.name]) for s in regions[region])
            region_caps.append(cap)
            region_mins.append(min(cap,max(floor,150000)))
        if sum(region_caps)<world_total:
            # This should not occur with the normal map, but custom maps should
            # fail softly rather than reintroducing unconstrained island growth.
            factor=world_total/max(1,sum(region_caps))*1.02
            for name in state_caps:
                state_caps[name]=int(state_caps[name]*factor)+1
            region_caps=[sum(state_caps[s.name] for s in regions[r]) for r in region_names]
            region_mins=[min(region_caps[i],region_mins[i]) for i in range(len(region_names))]
        region_alloc=largest_remainder(world_total,region_weights,region_mins,region_caps)
        for region,region_total in zip(region_names,region_alloc):
            group=regions[region]
            weights=[]; mins=[]; caps=[]
            for s in group:
                cap=state_caps[s.name]
                weights.append((state_carrying_capacity(s)**1.02)*math.exp(rng.gauss(0,sigma*0.28)))
                mins.append(_population_floor(s,cap))
                caps.append(cap)
            alloc=largest_remainder(region_total,weights,mins,caps)
            for s,v in zip(group,alloc):
                s.target_population=v
    else:
        mode={"regional":"strategic","macro":"macro"}.get(scope,"strategic")
        for _,group in group_states(active,mode).items():
            total=sum(s.population for s in group)
            if total<=0:
                continue
            weights=[]; mins=[]; caps=[]
            for s in group:
                cap=_population_hard_cap(s)
                memory=max(1000,s.population)**0.05
                weights.append((state_carrying_capacity(s)**1.02)*memory*math.exp(rng.gauss(0,sigma*0.32)))
                mins.append(_population_floor(s,cap))
                caps.append(cap)
            if sum(caps)<total:
                factor=total/max(1,sum(caps))*1.02
                caps=[int(c*factor)+1 for c in caps]
                mins=[min(m,c) for m,c in zip(mins,caps)]
            alloc=largest_remainder(total,weights,mins,caps)
            for s,v in zip(group,alloc):
                s.target_population=v
    for s in states.values():
        if s.target_population==0:
            s.target_population=s.population

def scale_sizes(values: List[int], target: int) -> List[int]:
    if not values: return []
    if target<=0: return [1]*len(values)
    mins=[1]*len(values)
    return largest_remainder(target,[max(1,v) for v in values],mins)


def _copy_pop_entries(entries: Sequence[PopEntry]) -> List[PopEntry]:
    return [PopEntry(e.culture,e.size,e.religion,e.pop_type) for e in entries]


def _culture_shares(entries: Sequence[PopEntry]) -> Counter:
    c=Counter()
    for e in entries:
        c[e.culture]+=e.size
    return c


def assign_cultural_blocks(
    states: Dict[str,StateData],
    pop_blocks: Dict[str,PopStateBlock],
    region_sequence: Sequence[str],
    rng: random.Random,
    intensity: str,
) -> Dict[str,List[str]]:
    """Permute complete POP compositions into contiguous cultural blocks and derive coherent homelands."""
    active=[name for name,pb in pop_blocks.items() if name in states and pb.total>0 and pb.entries]
    region_rank={r:i for i,r in enumerate(region_sequence)}
    targets=sorted(
        active,
        key=lambda n:(region_rank.get(states[n].strategic_region,9999),states[n].region_order,n),
    )
    by_culture=defaultdict(list)
    for name in active:
        culture=pop_blocks[name].dominant_culture or "unknown"
        by_culture[culture].append(name)

    # Large cultures form several separated homelands rather than one world-spanning ribbon.
    max_chunk={"low":14,"medium":10,"high":7}.get(intensity,10)
    chunks: List[Tuple[str,List[str]]] = []
    cultures=list(by_culture)
    rng.shuffle(cultures)
    for culture in cultures:
        names=by_culture[culture][:]
        rng.shuffle(names)
        while names:
            size=min(len(names),rng.randint(max(3,max_chunk//2),max_chunk))
            chunks.append((culture,names[:size]))
            names=names[size:]
    rng.shuffle(chunks)

    # Prevent adjacent chunks of the same culture where alternatives exist.
    for i in range(1,len(chunks)):
        if chunks[i][0]==chunks[i-1][0]:
            swap=next((j for j in range(i+1,len(chunks)) if chunks[j][0]!=chunks[i-1][0]),None)
            if swap is not None:
                chunks[i],chunks[swap]=chunks[swap],chunks[i]

    source_sequence=[name for _culture,chunk in chunks for name in chunk]
    if len(source_sequence)!=len(targets):
        raise ValueError("Cultural template allocation mismatch")

    # Match source and target population sizes inside each cultural chunk to minimize
    # distortion of global culture proportions while keeping each chunk contiguous.
    cursor=0
    homelands: Dict[str,List[str]] = {}
    for culture,chunk in chunks:
        segment=targets[cursor:cursor+len(chunk)]
        cursor+=len(chunk)
        sources=sorted(chunk,key=lambda n:pop_blocks[n].total)
        target_sorted=sorted(segment,key=lambda n:states[n].target_population)
        assignment=dict(zip(target_sorted,sources))
        for target in segment:
            source=assignment[target]
            entries=_copy_pop_entries(pop_blocks[source].entries)
            pop_blocks[target].assigned_entries=entries
            shares=_culture_shares(entries)
            total=sum(shares.values()) or 1
            chosen=[c for c,n in shares.most_common() if n/total>=0.22][:2]
            if not chosen and culture!="unknown":
                chosen=[culture]
            homelands[target]=chosen
    return homelands


def _format_pop_entry(entry: PopEntry, size: int, indent: str="\t\t\t") -> str:
    lines=[indent+"create_pop = {",indent+f"\tculture = {entry.culture}"]
    if entry.religion:
        lines.append(indent+f"\treligion = {entry.religion}")
    if entry.pop_type:
        lines.append(indent+f"\tpop_type = {entry.pop_type}")
    lines.append(indent+f"\tsize = {size}")
    lines.append(indent+"}")
    return "\n".join(lines)


def _format_pop_state_block(pb: PopStateBlock, target: int) -> str:
    entries=pb.assigned_entries or pb.entries
    if not entries:
        return pb.full_text[pb.block_start:pb.block_end]
    wrapper_weights=[max(1,x) for x in pb.wrapper_totals]
    wrapper_totals=largest_remainder(target,wrapper_weights,[1]*len(wrapper_weights))
    lines=[f"\n\ts:{pb.state} = {{"]
    for wrapper,total in zip(pb.wrapper_keys,wrapper_totals):
        lines.append(f"\t\t{wrapper} = {{")
        sizes=scale_sizes([e.size for e in entries],total)
        for e,size in zip(entries,sizes):
            lines.append(_format_pop_entry(e,size,"\t\t\t"))
        lines.append("\t\t}")
    lines.append("\t}")
    return "\n".join(lines)


def write_population_overrides_natural(
    states: Dict[str,StateData],
    pop_blocks: Dict[str,PopStateBlock],
    pop_texts: Dict[Path,str],
    game_root: Path,
    mod_root: Path,
    cultural_blocks: bool,
) -> None:
    ops_by_file=defaultdict(list)
    if cultural_blocks:
        for name,pb in pop_blocks.items():
            if name not in states:
                continue
            replacement=_format_pop_state_block(pb,states[name].target_population)
            ops_by_file[pb.path].append((pb.block_start,pb.block_end,replacement))
    else:
        for name,pb in pop_blocks.items():
            if name not in states:
                continue
            target=states[name].target_population
            oldvals=[v for _,_,v in pb.size_spans]
            newvals=scale_sizes(oldvals,target)
            for (a,b,_),nv in zip(pb.size_spans,newvals):
                ops_by_file[pb.path].append((a,b,str(nv)))
    for src,text in pop_texts.items():
        for a,b,r in sorted(ops_by_file[src],reverse=True):
            text=text[:a]+r+text[b:]
        write_text(mod_root/src.relative_to(game_root),text,bom=True)


def write_homeland_overrides(base: Path, mod_root: Path, homelands: Dict[str,List[str]]) -> None:
    src=base/'common/history/states/00_states.txt'
    if not src.exists() or not homelands:
        return
    text=read_text(src)
    outer=top_level_blocks(text,r"STATES")
    if not outer:
        return
    _,_,op,cl,_=outer[0]
    inner=text[op+1:cl]
    offset=op+1
    ops=[]
    for name,start,bop,bcl,end in top_level_blocks(inner,r"s:STATE_[A-Z0-9_]+"):
        state=name[2:]
        if state not in homelands:
            continue
        abs_start,abs_end=offset+start,offset+end
        block=text[abs_start:abs_end]
        indent_m=re.search(r"(?m)^(\s*)s:STATE_",block)
        indent=indent_m.group(1) if indent_m else "\t"
        for a,b in sorted(find_top_level_fields(block,"add_homeland"),reverse=True):
            block=block[:a]+block[b:]
        close_idx=matching_brace(block,block.find('{'))
        prefix=block[:close_idx].rstrip()
        values="\n".join(f"{indent}\tadd_homeland = cu:{culture}" for culture in homelands[state])
        new_block=prefix+"\n\n"+values+"\n"+indent+"}"
        ops.append((abs_start,abs_end,new_block))
    for a,b,r in sorted(ops,reverse=True):
        text=text[:a]+r+text[b:]
    write_text(mod_root/src.relative_to(base),text,bom=True)

# ================================================================
# File generation
# ================================================================

def format_arable_resources(values: List[str]) -> str:
    quoted=" ".join(f'"{x}"' for x in values)
    return f"    arable_resources = {{ {quoted} }}"


def format_capped(values: Dict[str,int]) -> Optional[str]:
    if not values: return None
    lines=["    capped_resources = {"]
    for b,n in sorted(values.items()): lines.append(f"        {b} = {n}")
    lines.append("    }")
    return "\n".join(lines)


def format_resource(r: ResourceRecord) -> str:
    lines=["    resource = {",f'        type = "{r.building}"']
    if r.depleted_type:
        lines.append(f'        depleted_type = "{r.depleted_type}"')
    lines.append(f"        {r.amount_key or 'undiscovered_amount'} = {r.amount}")
    if r.discovered_amount:
        lines.append(f"        discovered_amount = {r.discovered_amount}")
    lines.append("    }")
    return "\n".join(lines)


def write_state_region_overrides(states: Dict[str,StateData], file_texts: Dict[Path,str], file_blocks: Dict[Path,List[Tuple[str,int,int,int,int]]], game_root: Path, mod_root: Path, options: dict) -> None:
    for src,text in file_texts.items():
        ops=[]
        for name,start,op,cl,end in file_blocks[src]:
            s=states[name]
            block=text[start:end]
            reps={}
            repeated={}
            if options["arable_land"]!="keep": reps["arable_land"]=f"    arable_land = {s.new_arable_land}"
            if options["arable_resources"]!="keep": reps["arable_resources"]=format_arable_resources(s.new_arable_resources)
            if options["resources"]!="keep":
                reps["capped_resources"]=format_capped(s.new_capped)
                repeated["resource"]=[format_resource(r) for r in sorted(s.new_resources,key=lambda x:x.building)]
            if reps or repeated:
                new_block=replace_fields(block,reps,repeated)
                ops.append((start,end,new_block))
        for a,b,r in sorted(ops,reverse=True): text=text[:a]+r+text[b:]
        rel=src.relative_to(game_root)
        write_text(mod_root/rel,text,bom=True)


def write_discoverable_mining_override(base: Path, mod_root: Path, enabled: bool) -> None:
    """Enable native discovery for the ordinary mining group in gradual mode."""
    if not enabled:
        return
    src=base/'common/building_groups/00_building_groups.txt'
    if not src.exists():
        raise FileNotFoundError(f'Building group definitions not found: {src}')
    text=read_text(src)
    blocks=top_level_blocks(text,r"bg_mining")
    if len(blocks)!=1:
        raise ValueError('Could not uniquely locate bg_mining')
    _name,start,_op,_cl,end=blocks[0]
    block=text[start:end]
    block=replace_fields(block,{"discoverable_resource":"	discoverable_resource = yes"})
    text=text[:start]+block+text[end:]
    write_text(mod_root/src.relative_to(base),text,bom=True)


def write_population_overrides(states: Dict[str,StateData], pop_blocks: Dict[str,PopStateBlock], pop_texts: Dict[Path,str], game_root: Path, mod_root: Path) -> None:
    ops_by_file=defaultdict(list)
    for name,pb in pop_blocks.items():
        if name not in states: continue
        target=states[name].target_population
        oldvals=[v for _,_,v in pb.size_spans]
        newvals=scale_sizes(oldvals,target)
        for (a,b,_),nv in zip(pb.size_spans,newvals): ops_by_file[pb.path].append((a,b,str(nv)))
    for src,text in pop_texts.items():
        for a,b,r in sorted(ops_by_file[src],reverse=True): text=text[:a]+r+text[b:]
        rel=src.relative_to(game_root)
        write_text(mod_root/rel,text,bom=True)

def parse_building_keys(building_dir: Path) -> List[str]:
    keys=set()
    for path in building_dir.glob("*.txt"):
        if path.name.endswith(".md"):
            continue
        text=read_text(path)
        keys.update(re.findall(r"(?m)^(building_[a-z0-9_]+)\s*=\s*\{",text))
    # Subsistence buildings are terrain-generated and should not be explicitly removed.
    return sorted(k for k in keys if not k.startswith("building_subsistence_"))


def parse_building_unlocks(building_dir: Path) -> Dict[str,List[str]]:
    result: Dict[str,List[str]]={}
    for path in sorted(building_dir.glob("*.txt")):
        if path.name.endswith(".md"): continue
        text=read_text(path)
        for name,start,op,cl,end in top_level_blocks(text,r"building_[a-z0-9_]+"):
            block=text[start:end]
            m=re.search(r"\bunlocking_technologies\s*=\s*\{([^{}]*)\}",block,re.S)
            if not m:
                result[name]=[]
                continue
            body=re.sub(r"#.*","",m.group(1))
            result[name]=re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b",body)
    return result


ENGINE_MANAGED_BUILDINGS = {
    'building_manor_house','building_financial_district','building_urban_center',
    'building_company_headquarters',
}

RURAL_HUB_BUILDINGS = [
    ('building_wheat_farm',5),('building_rye_farm',5),('building_rice_farm',5),('building_maize_farm',5),('building_millet_farm',5),
    ('building_livestock_ranch',7),('building_vineyard',2),('building_cotton_plantation',2),('building_sugar_plantation',2),
    ('building_tobacco_plantation',1),('building_coffee_plantation',1),('building_tea_plantation',1),('building_dye_plantation',1),
    ('building_opium_plantation',1),('building_banana_plantation',1),('building_silk_plantation',1),('building_logging_camp',7),
]
RESOURCE_HUB_BUILDINGS = [
    ('building_coal_mine',5),('building_iron_mine',5),('building_lead_mine',2),('building_sulfur_mine',2),
    ('building_gold_field',1),('building_gold_mine',1),('building_oil_rig',1),('building_rubber_plantation',2),
]
INDUSTRIAL_HUB_BUILDINGS = [
    ('building_food_industry',6),('building_textile_mill',6),('building_furniture_manufactory',6),
    ('building_tooling_workshop',7),('building_paper_mill',4),('building_glassworks',3),('building_shipyard',2),
    ('building_chemical_plant',2),('building_explosives_factory',1),('building_steel_mill',2),
    ('building_arms_industry',2),('building_artillery_foundry',1),('building_munition_plant',1),('building_motor_industry',1),
]

def make_remove_block(building_keys: Sequence[str], indent: str="        ") -> str:
    lines=["every_state = {",f"{indent}# Comprehensive removal based on the installed game building definitions."]
    for b in building_keys: lines.append(f"{indent}safe_remove_building = {{ BUILDING_KEY = {b} }}")
    lines.append("    }")
    return "\n".join(lines)


def _tech_limit_lines(building: str, unlocks: Dict[str,List[str]], indent: str) -> List[str]:
    techs=unlocks.get(building,[])
    if not techs: return []
    lines=[indent+"owner = {"]
    for tech in techs: lines.append(indent+f"    has_technology_researched = {tech}")
    lines.append(indent+"}")
    return lines


def _hub_building_lines(building: str, weight: int, budget_scope: str, denominator: int, unlocks: Dict[str,List[str]], indent: str="        ", max_level: Optional[int]=None) -> List[str]:
    lines=[
        indent+"save_scope_value_as = {",indent+"    name = bwg_level",indent+"    value = {",
        indent+f"        value = scope:{budget_scope}",indent+f"        multiply = {weight}",indent+f"        divide = {denominator}",
        indent+"        multiply = { 75 125 }",indent+"        divide = 100",indent+"        floor = yes",
    ]
    if max_level is not None:
        lines.append(indent+f"        max = {max_level}")
    lines += [indent+"    }",indent+"}",
        indent+"if = {",indent+"    limit = {",indent+"        scope:bwg_level > 0",indent+"        is_incorporated = yes",indent+"        owner = scope:bwg_country",indent+f"        can_construct_building = {building}",
    ]
    lines += _tech_limit_lines(building,unlocks,indent+"        ")
    lines += [
        indent+"    }",indent+"    create_building = {",indent+f'        building = "{building}"',
        indent+"        level = scope:bwg_level",indent+"        reserves = 1",indent+"    }",indent+"}",
    ]
    return lines


ARCHETYPE_RURAL = {
    "agrarian": [
        ('building_wheat_farm',5),('building_rye_farm',5),('building_rice_farm',5),('building_maize_farm',5),('building_millet_farm',5),
        ('building_livestock_ranch',7),('building_vineyard',2),('building_cotton_plantation',2),('building_sugar_plantation',2),
        ('building_tobacco_plantation',1),('building_coffee_plantation',1),('building_tea_plantation',1),('building_dye_plantation',1),
        ('building_opium_plantation',1),('building_banana_plantation',1),('building_silk_plantation',1),('building_logging_camp',4),
    ],
    "frontier": [
        ('building_livestock_ranch',8),('building_logging_camp',9),('building_wheat_farm',3),('building_rye_farm',3),
        ('building_maize_farm',3),('building_millet_farm',3),('building_rice_farm',3),
    ],
    "protoindustrial": [
        ('building_wheat_farm',2),('building_rye_farm',2),('building_rice_farm',2),('building_maize_farm',2),
        ('building_livestock_ranch',2),('building_logging_camp',5),('building_cotton_plantation',2),('building_silk_plantation',1),
    ],
    "mining": [('building_logging_camp',5),('building_livestock_ranch',2)],
    "maritime": [('building_logging_camp',3),('building_livestock_ranch',2),('building_wheat_farm',1),('building_rice_farm',1)],
}
ARCHETYPE_RESOURCE = {
    "mining": [('building_coal_mine',8),('building_iron_mine',8),('building_lead_mine',3),('building_sulfur_mine',3),('building_gold_field',1),('building_gold_mine',1)],
    "protoindustrial": [('building_coal_mine',4),('building_iron_mine',4),('building_lead_mine',1),('building_sulfur_mine',1)],
    "frontier": [('building_coal_mine',2),('building_iron_mine',2),('building_gold_field',1),('building_gold_mine',1)],
    "agrarian": [('building_iron_mine',1),('building_sulfur_mine',1)],
    "maritime": [('building_iron_mine',1),('building_coal_mine',1)],
}
ARCHETYPE_INDUSTRY = {
    "protoindustrial": [('building_tooling_workshop',9),('building_textile_mill',7),('building_furniture_manufactory',7),('building_food_industry',6),('building_paper_mill',4),('building_glassworks',3),('building_steel_mill',2),('building_motor_industry',1)],
    "mining": [('building_tooling_workshop',8),('building_steel_mill',4),('building_arms_industry',2),('building_food_industry',2),('building_furniture_manufactory',2)],
    "maritime": [('building_shipyard',7),('building_food_industry',4),('building_tooling_workshop',4),('building_textile_mill',3),('building_furniture_manufactory',2)],
    "agrarian": [('building_food_industry',7),('building_textile_mill',3),('building_furniture_manufactory',3),('building_tooling_workshop',3)],
    "frontier": [('building_tooling_workshop',3),('building_food_industry',2),('building_furniture_manufactory',2)],
}


def _fixed_building_lines(building: str, level: int, unlocks: Dict[str,List[str]], indent: str="            ") -> List[str]:
    lines=[indent+"if = {",indent+"    limit = {",indent+"        is_incorporated = yes",indent+"        owner = scope:bwg_country",indent+f"        can_construct_building = {building}"]
    lines += _tech_limit_lines(building,unlocks,indent+"        ")
    lines += [indent+"    }",indent+"    create_building = {",indent+f'        building = "{building}"',indent+f"        level = {level}",indent+"        reserves = 1",indent+"    }",indent+"}"]
    return lines


def _archetype_scope_lines(archetype: str, unlocks: Dict[str,List[str]], gradual_resources: bool) -> List[str]:
    lines=["        if = {",f"            limit = {{ has_variable = bwg_arch_{archetype} }}","            scope:bwg_rural_hub = {"]
    for b,w in ARCHETYPE_RURAL[archetype]:
        lines += _hub_building_lines(b,w,'bwg_rural_budget',30,unlocks,'                ')
    lines += ["            }","            scope:bwg_resource_hub = {"]
    for b,w in ARCHETYPE_RESOURCE[archetype]:
        cap=1 if gradual_resources and b in GRADUAL_MINERAL_BUILDINGS else None
        lines += _hub_building_lines(b,w,'bwg_resource_budget',22,unlocks,'                ',cap)
    lines += ["            }","            scope:bwg_industrial_hub = {"]
    for b,w in ARCHETYPE_INDUSTRY[archetype]:
        lines += _hub_building_lines(b,w,'bwg_industry_budget',38,unlocks,'                ')
    lines += ["            }","        }"]
    return lines



def _script_value_lines(name: str, source: str, denominator: int, cap: int, indent: str="            ") -> List[str]:
    return [
        indent+"save_scope_value_as = {",
        indent+f"    name = {name}",
        indent+"    value = {",
        indent+f"        value = {source}",
        indent+f"        divide = {denominator}",
        indent+"        floor = yes",
        indent+f"        max = {cap}",
        indent+"        min = 0",
        indent+"    }",
        indent+"}",
    ]


def _random_available_building_lines(
    buildings: Sequence[str],
    level_scope: str,
    unlocks: Dict[str,List[str]],
    indent: str="            ",
) -> List[str]:
    """Create exactly one eligible building type at the calculated level.

    The empty fallback keeps the random list valid on custom maps where none of
    the listed buildings can be constructed.  Each branch is technology-gated.
    """
    lines=[indent+"if = {", indent+"    limit = {", indent+f"        scope:{level_scope} > 0", indent+"    }", indent+"    random_list = {"]
    lines.append(indent+"        1 = { }")
    for building in buildings:
        lines += [
            indent+"        4 = {",
            indent+"            trigger = {",
            indent+"                is_incorporated = yes",
            indent+"                owner = scope:bwg_country",
            indent+f"                can_construct_building = {building}",
        ]
        lines += _tech_limit_lines(building, unlocks, indent+"                ")
        lines += [
            indent+"            }",
            indent+"            create_building = {",
            indent+f'                building = "{building}"',
            indent+f"                level = scope:{level_scope}",
            indent+"                reserves = 1",
            indent+"            }",
            indent+"        }",
        ]
    lines += [indent+"    }", indent+"}"]
    return lines


def _single_building_if_lines(
    building: str,
    level_scope: str,
    unlocks: Dict[str,List[str]],
    indent: str="            ",
    extra_limits: Optional[Sequence[str]]=None,
) -> List[str]:
    lines=[
        indent+"if = {",
        indent+"    limit = {",
        indent+f"        scope:{level_scope} > 0",
        indent+"        is_incorporated = yes",
        indent+"        owner = scope:bwg_country",
        indent+f"        can_construct_building = {building}",
    ]
    if extra_limits:
        lines += [indent+"        "+x for x in extra_limits]
    lines += _tech_limit_lines(building, unlocks, indent+"        ")
    lines += [
        indent+"    }",
        indent+"    create_building = {",
        indent+f'        building = "{building}"',
        indent+f"        level = scope:{level_scope}",
        indent+"        reserves = 1",
        indent+"    }",
        indent+"}",
    ]
    return lines


def _random_eligible_building_lines_v614(
    buildings: Sequence[str],
    level_scope: str,
    indent: str="            ",
    empty_weight: int=1,
    branch_weight: int=100,
) -> List[str]:
    """Select one geographically valid building without an extra technology filter.

    v6.14 gives centralized countries a small common 1836 technology floor before
    this helper runs.  ``can_construct_building`` therefore tests the state
    potential and country laws rather than silently discarding most of the world.
    """
    lines=[
        indent+"if = {",
        indent+"    limit = {",
        indent+f"        scope:{level_scope} > 0",
        indent+"        is_incorporated = yes",
        indent+"        owner = scope:bwg_country",
        indent+"    }",
        indent+"    random_list = {",
        indent+f"        {empty_weight} = {{ }}",
    ]
    for building in buildings:
        lines += [
            indent+f"        {branch_weight} = {{",
            indent+"            trigger = {",
            indent+f"                can_construct_building = {building}",
            indent+"            }",
            indent+"            create_building = {",
            indent+f'                building = "{building}"',
            indent+f"                level = scope:{level_scope}",
            indent+"                reserves = 1",
            indent+"            }",
            indent+"        }",
        ]
    lines += [indent+"    }", indent+"}"]
    return lines


def _v614_create_if_possible(building: str, level: int=1, indent: str="            ", extra_limits: Optional[Sequence[str]]=None) -> List[str]:
    lines=[
        indent+"if = {",
        indent+"    limit = {",
        indent+"        is_incorporated = yes",
        indent+"        owner = scope:bwg_country",
        indent+f"        can_construct_building = {building}",
    ]
    if extra_limits:
        lines += [indent+"        "+x for x in extra_limits]
    lines += [
        indent+"    }",
        indent+f'    create_building = {{ building = "{building}" level = {level} reserves = 1 }}',
        indent+"}",
    ]
    return lines


def _v614_chance_building(building: str, chance_weight: int, indent: str="            ", extra_limits: Optional[Sequence[str]]=None) -> List[str]:
    limits=["is_incorporated = yes", "owner = scope:bwg_country", f"can_construct_building = {building}"]
    if extra_limits:
        limits.extend(extra_limits)
    lines=[indent+"random_list = {", indent+"    3 = { }"]
    lines += [indent+f"    {chance_weight} = {{", indent+"        trigger = {"]
    lines += [indent+"            "+x for x in limits]
    lines += [
        indent+"        }",
        indent+f'        create_building = {{ building = "{building}" level = 1 reserves = 1 }}',
        indent+"    }",
        indent+"}",
    ]
    return lines


def make_balanced_buildings_effect(development: str, unlocks: Dict[str,List[str]], gradual_resources: bool=False, fiscal_safety: str="strict") -> str:
    """Build a playable civil economy, a minimal commercial network and shipping.

    v6.15 keeps the state-by-state economy from v6.14, but adds the missing
    commercial layer: trade centres, basic ports that actually produce merchant
    marine, and a sparse civilian shipbuilding network.  Consumer industries are
    scaled by country size so multi-state countries do not start with one lonely
    factory for the whole market.
    """
    factor={"very_low":1.18,"low":1.08,"normal":1.0,"high":0.90}.get(development,1.0)
    profiles={
        "strict": {
            "food_den":340000,"food_cap":7,
            "rural_den":900000,"rural_cap":2,
            "resource_min":330000,"second_resource_min":1500000,
            "cash_min":600000,"fish_min":240000,
            "core_min":90000,"textile_min":250000,"furniture_min":340000,
            "paper_min":950000,"glass_min":1500000,
            "construction_min":360000,"admin_min":220000,"university_min":4000000,
            "port_min":280000,"port_cap":2,
            "trade_min":70000,"trade_cap":2,
            "rail_min":3800000,
            "shipyard_min":850000,"shipyard_large":2400000,"shipyard_chance":2,
            "core_cap":3,"consumer_cap":2,
            "resource_chance":7,"secondary_chance":4,
        },
        "balanced": {
            "food_den":290000,"food_cap":8,
            "rural_den":700000,"rural_cap":3,
            "resource_min":250000,"second_resource_min":1000000,
            "cash_min":430000,"fish_min":175000,
            "core_min":70000,"textile_min":190000,"furniture_min":260000,
            "paper_min":700000,"glass_min":1100000,
            "construction_min":260000,"admin_min":150000,"university_min":2800000,
            "port_min":220000,"port_cap":3,
            "trade_min":50000,"trade_cap":3,
            "rail_min":2800000,
            "shipyard_min":650000,"shipyard_large":1800000,"shipyard_chance":3,
            "core_cap":4,"consumer_cap":3,
            "resource_chance":9,"secondary_chance":6,
        },
        "legacy": {
            "food_den":230000,"food_cap":10,
            "rural_den":500000,"rural_cap":4,
            "resource_min":160000,"second_resource_min":650000,
            "cash_min":280000,"fish_min":110000,
            "core_min":50000,"textile_min":120000,"furniture_min":180000,
            "paper_min":450000,"glass_min":700000,
            "construction_min":160000,"admin_min":90000,"university_min":1800000,
            "port_min":140000,"port_cap":4,
            "trade_min":50000,"trade_cap":4,
            "rail_min":1900000,
            "shipyard_min":420000,"shipyard_large":1200000,"shipyard_chance":4,
            "core_cap":5,"consumer_cap":4,
            "resource_chance":12,"secondary_chance":9,
        },
    }
    p=dict(profiles.get(fiscal_safety,profiles["strict"]))
    for key in list(p):
        if key.endswith('_den') or key.endswith('_min') or key.endswith('_large'):
            p[key]=max(1,int(p[key]*factor))

    staples=["building_wheat_farm","building_rye_farm","building_rice_farm","building_maize_farm","building_millet_farm"]
    cash_crops=["building_cotton_plantation","building_sugar_plantation","building_tobacco_plantation","building_coffee_plantation","building_tea_plantation","building_dye_plantation","building_opium_plantation","building_banana_plantation","building_silk_plantation","building_vineyard"]
    basic_mines=["building_iron_mine","building_coal_mine","building_lead_mine","building_sulfur_mine"]

    lines=[
        "balanced_world_generate_buildings = {",
        "    every_country = {",
        "        limit = { NOT = { is_country_type = decentralized } }",
        "        save_scope_as = bwg_country",
        "",
        "        # Minimal common technology floor for a functioning 1836 economy.",
    ]
    for tech in ("enclosure","manufacturies","shaft_mining","urbanization","tech_bureaucracy"):
        lines += [
            "        if = {",
            f"            limit = {{ NOT = {{ has_technology_researched = {tech} }} }}",
            f"            add_technology_researched = {tech}",
            "        }",
        ]
    lines += [
        "        if = {",
        "            limit = {",
        "                any_scope_state = { is_incorporated = yes is_coastal = yes }",
        "                NOT = { has_technology_researched = navigation }",
        "            }",
        "            add_technology_researched = navigation",
        "        }",
        "",
        "        every_scope_state = {",
        "            limit = { is_incorporated = yes owner = scope:bwg_country state_population >= 50000 }",
        "",
        "            # Staples are proportional to local population rather than a single token farm.",
        "            save_scope_value_as = {",
        "                name = bwg_food_level",
        "                value = {",
        "                    value = state_population",
        f"                    divide = {p['food_den']}",
        "                    floor = yes",
        "                    add = 1",
        f"                    max = {p['food_cap']}",
        "                    min = 1",
        "                }",
        "            }",
    ]
    lines += _random_eligible_building_lines_v614(staples,'bwg_food_level','            ',empty_weight=1,branch_weight=100)
    lines += [
        "            save_scope_value_as = {",
        "                name = bwg_rural_level",
        "                value = {",
        "                    value = state_population",
        f"                    divide = {p['rural_den']}",
        "                    floor = yes",
        "                    add = 1",
        f"                    max = {p['rural_cap']}",
        "                    min = 1",
        "                }",
        "            }",
        "            if = {",
        "                limit = { is_incorporated = yes owner = scope:bwg_country can_construct_building = building_livestock_ranch }",
        "                create_building = { building = \"building_livestock_ranch\" level = scope:bwg_rural_level reserves = 1 }",
        "            }",
        "            if = {",
        "                limit = { is_incorporated = yes owner = scope:bwg_country can_construct_building = building_logging_camp }",
        "                create_building = { building = \"building_logging_camp\" level = scope:bwg_rural_level reserves = 1 }",
        "            }",
    ]
    lines += _v614_create_if_possible('building_fishing_wharf',1,'            ',["is_coastal = yes",f"state_population >= {p['fish_min']}"])

    for b in basic_mines:
        chance=p['resource_chance']
        if gradual_resources and b in GRADUAL_MINERAL_BUILDINGS:
            chance=max(4,chance-2)
        lines += _v614_chance_building(b,chance,'            ',[f"state_population >= {p['resource_min']}"])
    for b in basic_mines:
        chance=p['secondary_chance']
        if gradual_resources and b in GRADUAL_MINERAL_BUILDINGS:
            chance=max(2,chance-2)
        lines += _v614_chance_building(b,chance,'            ',[f"state_population >= {p['second_resource_min']}"])

    lines += [
        "            if = {",
        f"                limit = {{ state_population >= {p['cash_min']} }}",
        "                save_scope_value_as = { name = bwg_cash_level value = 1 }",
    ]
    lines += _random_eligible_building_lines_v614(cash_crops,'bwg_cash_level','                ',empty_weight=5,branch_weight=30)
    lines += [
        "            }",
        "            if = {",
        "                limit = {",
        f"                    state_population >= {p['rail_min']}",
        "                    owner = { has_technology_researched = railways }",
        "                    can_construct_building = building_railway",
        "                }",
        "                create_building = { building = \"building_railway\" level = 1 reserves = 1 }",
        "            }",
        "        }",
        "",
        "        # Country-sized consumer core: multi-state countries receive more than one factory.",
        "        capital = {",
        "            save_scope_value_as = {",
        "                name = bwg_core_level",
        "                value = {",
        "                    value = 1",
        "                    if = { limit = { state_population >= 900000 } add = 1 }",
        "                    if = { limit = { owner = { num_states >= 3 } } add = 1 }",
        "                    if = { limit = { owner = { num_states >= 7 } } add = 1 }",
        f"                    max = {p['core_cap']}",
        "                    min = 1",
        "                }",
        "            }",
        "            save_scope_value_as = {",
        "                name = bwg_consumer_level",
        "                value = {",
        "                    value = 1",
        "                    if = { limit = { state_population >= 1400000 } add = 1 }",
        "                    if = { limit = { owner = { num_states >= 5 } } add = 1 }",
        f"                    max = {p['consumer_cap']}",
        "                    min = 1",
        "                }",
        "            }",
        "            if = {",
        f"                limit = {{ state_population >= {p['core_min']} can_construct_building = building_food_industry }}",
        "                create_building = { building = \"building_food_industry\" level = scope:bwg_core_level reserves = 1 }",
        "            }",
        "            if = {",
        f"                limit = {{ state_population >= {p['core_min']} can_construct_building = building_tooling_workshop }}",
        "                create_building = { building = \"building_tooling_workshop\" level = scope:bwg_core_level reserves = 1 }",
        "            }",
        "            if = {",
        f"                limit = {{ state_population >= {p['textile_min']} can_construct_building = building_textile_mill }}",
        "                create_building = { building = \"building_textile_mill\" level = scope:bwg_consumer_level reserves = 1 }",
        "            }",
        "            if = {",
        f"                limit = {{ state_population >= {p['furniture_min']} can_construct_building = building_furniture_manufactory }}",
        "                create_building = { building = \"building_furniture_manufactory\" level = scope:bwg_consumer_level reserves = 1 }",
        "            }",
    ]
    lines += _v614_create_if_possible('building_paper_mill',1,'            ',[f"state_population >= {p['paper_min']}"])
    lines += _v614_create_if_possible('building_glassworks',1,'            ',[f"state_population >= {p['glass_min']}"])
    lines += _v614_create_if_possible('building_construction_sector',1,'            ',[f"state_population >= {p['construction_min']}"])
    lines += _v614_create_if_possible('building_government_administration',1,'            ',[f"state_population >= {p['admin_min']}"])
    lines += _v614_create_if_possible('building_university',1,'            ',[f"state_population >= {p['university_min']}"])
    lines += [
        "",
        "            # Every centralized country starts with a small commercial hub.",
        "            save_scope_value_as = {",
        "                name = bwg_trade_level",
        "                value = {",
        "                    value = 1",
        "                    if = { limit = { state_population >= 1800000 } add = 1 }",
        "                    if = { limit = { owner = { num_states >= 6 } } add = 1 }",
        f"                    max = {p['trade_cap']}",
        "                    min = 1",
        "                }",
        "            }",
        "            if = {",
        "                limit = {",
        f"                    state_population >= {p['trade_min']}",
        "                    can_construct_building = building_trade_center",
        "                }",
        "                create_building = {",
        "                    building = \"building_trade_center\"",
        "                    level = scope:bwg_trade_level",
        "                    reserves = 1",
        "                    activate_production_methods = { \"pm_trade_center\" \"pm_trade_center_trade_quantity_limited\" }",
        "                }",
        "            }",
        "        }",
        "",
        "        # Ports use the basic-port PM explicitly; anchorage alone produces no merchant marine.",
        "        save_scope_value_as = {",
        "            name = bwg_port_level",
        "            value = {",
        "                value = 1",
        "                if = { limit = { num_states >= 5 } add = 1 }",
        "                if = { limit = { num_states >= 10 } add = 1 }",
        f"                max = {p['port_cap']}",
        "                min = 1",
        "            }",
        "        }",
        "        if = {",
        "            limit = {",
        f"                capital ?= {{ state_population >= {p['port_min']} }}",
        "                any_scope_state = { is_incorporated = yes is_coastal = yes can_construct_building = building_port }",
        "            }",
        "            random_scope_state = {",
        "                limit = { is_incorporated = yes is_coastal = yes can_construct_building = building_port }",
        "                create_building = {",
        "                    building = \"building_port\"",
        "                    level = scope:bwg_port_level",
        "                    reserves = 1",
        "                    activate_production_methods = { \"pm_basic_port\" }",
        "                }",
        "            }",
        "        }",
        "",
        "        # Sparse civilian shipbuilding supplies clippers without granting a navy.",
        "        if = {",
        "            limit = {",
        "                country_has_building_type_levels = { target = bt:building_port value >= 1 }",
        "                country_has_building_type_levels = { target = bt:building_logging_camp value >= 1 }",
        "                OR = {",
        "                    country_has_building_type_levels = { target = bt:building_livestock_ranch value >= 1 }",
        "                    country_has_building_type_levels = { target = bt:building_textile_mill value >= 1 }",
        "                    country_has_building_type_levels = { target = bt:building_cotton_plantation value >= 1 }",
        "                }",
        f"                any_scope_state = {{ is_incorporated = yes is_coastal = yes state_population >= {p['shipyard_min']} can_construct_building = building_shipyard }}",
        "            }",
        "            if = {",
        "                limit = {",
        "                    OR = {",
        "                        num_states >= 5",
        f"                        any_scope_state = {{ is_incorporated = yes is_coastal = yes state_population >= {p['shipyard_large']} }}",
        "                    }",
        "                }",
        "                random_scope_state = {",
        f"                    limit = {{ is_incorporated = yes is_coastal = yes state_population >= {p['shipyard_min']} can_construct_building = building_shipyard }}",
        "                    create_building = { building = \"building_shipyard\" level = 1 reserves = 1 activate_production_methods = { \"pm_basic_shipbuilding\" } }",
        "                }",
        "            }",
        "            else = {",
        "                random_list = {",
        "                    8 = { }",
        f"                    {p['shipyard_chance']} = {{",
        "                        random_scope_state = {",
        f"                            limit = {{ is_incorporated = yes is_coastal = yes state_population >= {p['shipyard_min']} can_construct_building = building_shipyard }}",
        "                            create_building = { building = \"building_shipyard\" level = 1 reserves = 1 activate_production_methods = { \"pm_basic_shipbuilding\" } }",
        "                        }",
        "                    }",
        "                }",
        "            }",
        "        }",
        "    }",
        "}",
        "",
    ]
    return '\n'.join(lines)


MILITARY_ECONOMY_PROFILES = {
    "none": {
        "label": "sem forças militares automáticas",
        "army_min_pop": 999_000_000,
        "army_den": 999_000_000,
        "army_cap": 0,
        "navy_min_pop": 999_000_000,
        "navy_den": 999_000_000,
        "navy_cap": 0,
        "shipyard_den": 999_000_000,
        "shipyard_cap": 0,
        "capital_min_pop": 999_000_000,
    },
    "economic_conservative": {
        "label": "avaliação econômica conservadora",
        "army_min_pop": 1_350_000,
        "army_den": 1_350_000,
        "army_cap": 5,
        "navy_min_pop": 1_600_000,
        "navy_den": 1_600_000,
        "navy_cap": 2,
        "shipyard_den": 1_600_000,
        "shipyard_cap": 2,
        "capital_min_pop": 999_000_000,
    },
    "economic_balanced": {
        "label": "avaliação econômica balanceada",
        "army_min_pop": 900_000,
        "army_den": 900_000,
        "army_cap": 8,
        "navy_min_pop": 1_050_000,
        "navy_den": 1_050_000,
        "navy_cap": 3,
        "shipyard_den": 1_050_000,
        "shipyard_cap": 3,
        "capital_min_pop": 50_000_000,
    },
    "economic_strong": {
        "label": "avaliação econômica permissiva",
        "army_min_pop": 550_000,
        "army_den": 550_000,
        "army_cap": 12,
        "navy_min_pop": 700_000,
        "navy_den": 700_000,
        "navy_cap": 4,
        "shipyard_den": 700_000,
        "shipyard_cap": 4,
        "capital_min_pop": 30_000_000,
    },
}


def make_military_economy_effect(mode: str, unlocks: Dict[str,List[str]]) -> str:
    """Create small forces only after the v6.15 civil economy exists.

    Aptitude is evaluated from the capital population, territorial size, a
    domestic tools/resource chain and a single coastal hub.  The effect grants
    the institutional technology only to a country that already passed those
    economic tests, preventing a completely demilitarized world without restoring
    historical armies or England-specific bonuses.
    """
    profile=dict(MILITARY_ECONOMY_PROFILES.get(mode,MILITARY_ECONOMY_PROFILES['economic_conservative']))
    if mode=='none':
        return 'balanced_world_generate_military_economy = {\n}\n'
    army_cap=profile['army_cap']
    navy_cap=profile['navy_cap']
    ship_cap=profile['shipyard_cap']
    army_pop={
        'economic_conservative':1_350_000,
        'economic_balanced':900_000,
        'economic_strong':550_000,
    }.get(mode,1_350_000)
    navy_pop={
        'economic_conservative':1_600_000,
        'economic_balanced':1_050_000,
        'economic_strong':700_000,
    }.get(mode,1_600_000)
    lines=[
        'balanced_world_generate_military_economy = {',
        '    every_country = {',
        '        limit = { NOT = { is_country_type = decentralized } }',
        '        save_scope_as = bwg_military_country',
        '',
        '        capital = {',
        '            save_scope_value_as = {',
        '                name = bwg_army_level',
        '                value = {',
        '                    value = state_population',
        f'                    divide = {army_pop}',
        '                    floor = yes',
        '                    if = { limit = { owner = { num_states >= 3 } } add = 1 }',
        '                    if = { limit = { owner = { num_states >= 7 } } add = 1 }',
        f'                    max = {army_cap}',
        '                    min = 0',
        '                }',
        '            }',
        '            if = {',
        '                limit = {',
        '                    scope:bwg_army_level > 0',
        '                    owner = {',
        '                        country_has_building_type_levels = { target = bt:building_food_industry value >= 1 }',
        '                        country_has_building_type_levels = { target = bt:building_tooling_workshop value >= 1 }',
        '                        OR = {',
        '                            country_has_building_type_levels = { target = bt:building_iron_mine value >= 1 }',
        '                            country_has_building_type_levels = { target = bt:building_logging_camp value >= 1 }',
        '                            num_states >= 3',
        '                        }',
        '                    }',
        '                }',
        '                owner = {',
        '                    if = {',
        '                        limit = { NOT = { has_technology_researched = standing_army } }',
        '                        add_technology_researched = standing_army',
        '                    }',
        '                }',
        '                if = {',
        '                    limit = { can_construct_building = building_barrack }',
        '                    region = { save_temporary_scope_as = bwg_army_hq }',
        '                    create_building = { building = "building_barrack" level = scope:bwg_army_level reserves = 1 }',
        '                }',
        '            }',
        '            if = {',
        '                limit = {',
        '                    scope:bwg_army_level >= 3',
        '                    owner = { country_has_building_type_levels = { target = bt:building_iron_mine value >= 1 } }',
        '                }',
        '                owner = {',
        '                    if = { limit = { NOT = { has_technology_researched = gunsmithing } } add_technology_researched = gunsmithing }',
        '                }',
        '                if = {',
        '                    limit = { can_construct_building = building_arms_industry }',
        '                    create_building = { building = "building_arms_industry" level = 1 reserves = 1 }',
        '                }',
        '            }',
        '        }',
        '        if = {',
        '            limit = { exists = scope:bwg_army_hq }',
        '            create_military_formation = { type = army hq_region = scope:bwg_army_hq }',
        '        }',
        '',
        '        if = {',
        '            limit = {',
        '                country_has_building_type_levels = { target = bt:building_port value >= 1 }',
        '                country_has_building_type_levels = { target = bt:building_logging_camp value >= 1 }',
        '                country_has_building_type_levels = { target = bt:building_tooling_workshop value >= 1 }',
        '                OR = {',
        '                    country_has_building_type_levels = { target = bt:building_livestock_ranch value >= 1 }',
        '                    country_has_building_type_levels = { target = bt:building_cotton_plantation value >= 1 }',
        '                    country_has_building_type_levels = { target = bt:building_textile_mill value >= 1 }',
        '                }',
        '                any_scope_state = { is_incorporated = yes is_coastal = yes state_population >= '+str(navy_pop)+' }',
        '            }',
        '            if = { limit = { NOT = { has_technology_researched = admiralty } } add_technology_researched = admiralty }',
        '            random_scope_state = {',
        '                limit = {',
        '                    is_incorporated = yes',
        '                    is_coastal = yes',
        f'                    state_population >= {navy_pop}',
        '                    can_construct_building = building_shipyard',
        '                    can_construct_building = building_naval_administration',
        '                }',
        '                save_scope_value_as = {',
        '                    name = bwg_navy_level',
        '                    value = {',
        '                        value = 1',
        '                        if = { limit = { state_population >= 3500000 } add = 1 }',
        '                        if = { limit = { owner = { num_states >= 7 } } add = 1 }',
        f'                        max = {navy_cap}',
        '                        min = 1',
        '                    }',
        '                }',
        '                save_scope_value_as = {',
        '                    name = bwg_shipyard_level',
        '                    value = {',
        '                        value = scope:bwg_navy_level',
        f'                        max = {ship_cap}',
        '                        min = 1',
        '                    }',
        '                }',
        '                region = { save_temporary_scope_as = bwg_navy_hq }',
        '                if = {',
        '                    limit = { owner = { NOT = { country_has_building_type_levels = { target = bt:building_shipyard value >= 1 } } } }',
        '                    create_building = { building = "building_shipyard" level = 1 reserves = 1 activate_production_methods = { "pm_basic_shipbuilding" } }',
        '                }',
        '                create_building = { building = "building_naval_administration" level = scope:bwg_navy_level reserves = 1 }',
        '            }',
        '            if = {',
        '                limit = { exists = scope:bwg_navy_hq }',
        '                create_military_formation = { type = fleet hq_region = scope:bwg_navy_hq }',
        '                random_scope_fleet = { save_scope_as = bwg_generated_fleet }',
        '                while = {',
        '                    count = scope:bwg_navy_level',
        '                    create_ship = { type = ship_type:ship_type_frigate fleet = scope:bwg_generated_fleet }',
        '                }',
        '            }',
        '        }',
        '    }',
        '}',
        '',
    ]
    return '\n'.join(lines)


def strip_original_initial_buildings(mod_root: Path) -> None:
    """The source randomizer creates several buildings unconditionally; the new generator owns this step."""
    fx=mod_root/'common/scripted_effects/02_random_stuff.txt'
    text=read_text(fx)
    while True:
        m=re.search(r"(?m)^[ \t]*create_building\s*=\s*\{",text)
        if not m: break
        op=text.find('{',m.start(),m.end())
        end=matching_brace(text,op)+1
        # Consume the trailing newline so no empty indented fragments remain.
        if end<len(text) and text[end]=='\r': end+=1
        if end<len(text) and text[end]=='\n': end+=1
        text=text[:m.start()]+text[end:]
    if brace_balance(text)!=0: raise ValueError('Original building removal unbalanced')
    write_text(fx,text,bom=True)



def _remove_enclosing_every_country(text: str, marker: str) -> tuple[str,int]:
    """Remove every_country blocks that contain a dangerous generated-military marker."""
    removed=0
    while marker in text:
        pos=text.index(marker)
        candidates=[]
        for m in re.finditer(r"(?m)^[ \t]*every_country\s*=\s*\{",text[:pos]):
            op=text.find('{',m.start(),m.end())
            try:
                close=matching_brace(text,op)
            except Exception:
                continue
            if close>pos:
                candidates.append((m.start(),close+1))
        if not candidates:
            break
        a,b=candidates[-1]
        if b<len(text) and text[b]=='\r': b+=1
        if b<len(text) and text[b]=='\n': b+=1
        text=text[:a]+text[b:]
        removed+=1
    return text,removed


def strip_original_initial_military(mod_root: Path) -> int:
    """Remove legacy fleet/ship creation that ignored fiscal capacity."""
    fx=mod_root/'common/scripted_effects/02_random_stuff.txt'
    text=read_text(fx)
    total=0
    for marker in ('create_military_formation = {','create_ship = {'):
        text,n=_remove_enclosing_every_country(text,marker)
        total+=n
    # Safety net: remove any remaining standalone creation blocks.
    for token in ('create_military_formation','create_ship'):
        while True:
            m=re.search(rf"(?m)^[ \t]*{token}\s*=\s*\{{",text)
            if not m: break
            op=text.find('{',m.start(),m.end()); end=matching_brace(text,op)+1
            if end<len(text) and text[end]=='\r': end+=1
            if end<len(text) and text[end]=='\n': end+=1
            text=text[:m.start()]+text[end:]
            total+=1
    if brace_balance(text)!=0:
        raise ValueError('Original military removal unbalanced')
    write_text(fx,text,bom=True)
    return total

def patch_building_effect(mod_root: Path, default_buildings: str, development: str, building_keys: Sequence[str], unlocks: Dict[str,List[str]], gradual_resources: bool=False, fiscal_safety: str="strict", military_economy: str="economic_conservative") -> None:
    strip_original_initial_buildings(mod_root)
    strip_original_initial_military(mod_root)
    fx=mod_root/'common/scripted_effects/02_random_stuff.txt'
    text=read_text(fx)
    marker='randomise_buildings_effect = {'
    start=text.index(marker); op=text.index('{',start); end=matching_brace(text,op)+1
    remove=make_remove_block(building_keys)
    if default_buildings=='remove':
        body=['randomise_buildings_effect = {',textwrap.indent(remove,'    '),'}']
    elif default_buildings=='keep':
        body=['randomise_buildings_effect = {','}']
    else:
        body=[
            'randomise_buildings_effect = {',
            textwrap.indent(remove,'    '),
            '    balanced_world_generate_buildings = yes',
            '    balanced_world_generate_military_economy = yes',
            '}',
        ]
    new_func='\n'.join(body)
    text=text[:start]+new_func+text[end:]
    if brace_balance(text)!=0: raise ValueError('Patched building effect unbalanced')
    write_text(fx,text,bom=True)
    write_text(mod_root/'common/scripted_effects/99_balanced_world_generated_buildings.txt',make_balanced_buildings_effect(development,unlocks,gradual_resources,fiscal_safety),bom=True)
    write_text(mod_root/'common/scripted_effects/99_balanced_world_generated_military.txt',make_military_economy_effect(military_economy,unlocks),bom=True)
    gp=mod_root/'common/game_rules/01_randomiser_game_rules.txt'
    gt=read_text(gp)
    default_map={'remove':'randomiser_remove_buildings','balanced':'randomiser_randomise_buildings','keep':'randomiser_keep_buildings'}
    gt=re.sub(r'(?m)^\s*default\s*=\s*\w+',f"\tdefault = {default_map.get(default_buildings,'randomiser_randomise_buildings')}",gt,count=1)
    gt='# Informativo: a escolha efetiva foi gravada pelo gerador da seed; alterar esta regra no lobby não muda o mod.\n'+gt
    write_text(gp,gt,bom=True)

COUNTRY_SCALE_SETTINGS = {
    "fragmented": {
        "scale": 1.0,
        "cluster_probabilities": [],
        "label": "muitos países pequenos; comportamento próximo ao randomizador original",
    },
    "balanced": {
        "scale": 0.65,
        "cluster_probabilities": [70, 35],
        "label": "mosaico equilibrado, com menos cortes isolados e países de tamanhos variados",
    },
    "vanilla_like": {
        "scale": 0.38,
        "cluster_probabilities": [100, 75, 45],
        "label": "menos países e territórios compactos maiores, semelhante à escala política do jogo-base",
    },
    "large_blocks": {
        "scale": 0.20,
        "cluster_probabilities": [100, 90, 75, 55, 35],
        "label": "poucos países e grandes blocos territoriais contíguos",
    },
}


def _country_direct_field_span(block: str, key: str) -> Optional[Tuple[int, int]]:
    open_brace = block.find("{")
    close_brace = matching_brace(block, open_brace)
    rx = re.compile(rf"(?m)^[ \t]*{re.escape(key)}\s*=\s*")
    for match in rx.finditer(block, open_brace + 1, close_brace):
        if brace_balance(block[open_brace + 1:match.start()]) != 0:
            continue
        value_start = match.end()
        while value_start < close_brace and block[value_start] in " \t":
            value_start += 1
        if value_start < close_brace and block[value_start] == "{":
            end = matching_brace(block, value_start) + 1
        else:
            newline = block.find("\n", value_start, close_brace)
            end = close_brace if newline == -1 else newline
        return match.start(), end
    return None


def _scaled_country_spawn_range(old_min: int, old_max: int, scale: float) -> Tuple[int, int]:
    if scale >= 0.999:
        return old_min, old_max
    new_min = 0 if old_min == 0 else max(1, int(round(old_min * scale)))
    new_max = 0 if old_max == 0 else max(1, int(round(old_max * scale)))
    if new_max < new_min:
        new_max = new_min
    return new_min, new_max


def _country_cluster_growth(probabilities: Sequence[int], indent: str, level: int = 0) -> str:
    """Return a contiguous same-owner expansion chain for state_to_cede.

    Each successful step is scoped from the previously selected state, so the
    resulting dynamic country is a connected path rather than a collection of
    unrelated states. Missing eligible neighbours simply stop the chain.
    """
    if level >= len(probabilities):
        return ""
    probability = max(0, min(100, int(probabilities[level])))
    nested = _country_cluster_growth(probabilities, indent + "        ", level + 1)
    action_lines = [
        f"{indent}random_neighbouring_state = {{",
        f"{indent}    limit = {{",
        f"{indent}        owner = {{ scope:country ?= this }}",
        f"{indent}        NOT = {{ has_variable = state_to_cede }}",
        f"{indent}    }}",
        f"{indent}    set_variable = state_to_cede",
    ]
    if nested:
        action_lines.append(nested)
    action_lines.append(f"{indent}}}")
    action = "\n".join(action_lines)
    if probability >= 100:
        return action
    if probability <= 0:
        return ""
    return "\n".join([
        f"{indent}random_list = {{",
        f"{indent}    {100 - probability} = {{}}",
        f"{indent}    {probability} = {{",
        textwrap.indent(action, "    "),
        f"{indent}    }}",
        f"{indent}}}",
    ])


def patch_country_scale_effect(mod_root: Path, mode: str) -> dict:
    """Control the number and average territorial size of procedural countries.

    The template creates one-state countries by repeatedly cutting random states
    out of historical owners. This patch reduces the number of those cuts and,
    in the less-fragmented modes, cedes a connected cluster of same-owner states
    to each new country. It therefore changes both country count and compactness
    without inventing non-adjacent territory.
    """
    if mode not in COUNTRY_SCALE_SETTINGS:
        raise ValueError(f"Modo de tamanho dos países inválido: {mode}")
    settings = COUNTRY_SCALE_SETTINGS[mode]
    fx = mod_root / "common/scripted_effects/02_random_stuff.txt"
    text = read_text(fx)
    while_matches = list(re.finditer(r"(?m)^([ \t]*)while\s*=\s*\{", text))
    operations: List[Tuple[int, int, str]] = []
    loop_stats: List[Tuple[int, int, int, int]] = []
    candidates: List[Tuple[re.Match, int, int]] = []
    for match in while_matches:
        open_brace = text.find("{", match.start(), match.end())
        close_brace = matching_brace(text, open_brace)
        block = text[match.start():close_brace + 1]
        if "create_dynamic_country" in block:
            candidates.append((match, match.start(), close_brace + 1))
    # Some continent sections wrap the actual spawn loop in an outer repeat loop.
    # Patch only the innermost loops, otherwise overlapping replacements corrupt
    # the Clausewitz script and the outer repeat count is mistaken for country count.
    innermost = [
        candidate for candidate in candidates
        if not any(
            candidate[1] < other[1] and other[2] < candidate[2]
            for other in candidates
        )
    ]

    for match, block_start, block_end in innermost:
        block = text[block_start:block_end]

        count_span = _country_direct_field_span(block, "count")
        if not count_span:
            continue
        count_text = block[count_span[0]:count_span[1]]
        range_match = re.search(
            r"integer_range\s*=\s*\{\s*min\s*=\s*(\d+)\s*max\s*=\s*(\d+)",
            count_text,
            re.S,
        )
        scalar_match = re.search(r"count\s*=\s*(\d+)", count_text)
        if range_match:
            old_min, old_max = int(range_match.group(1)), int(range_match.group(2))
            scalar = False
        elif scalar_match:
            old_min = old_max = int(scalar_match.group(1))
            scalar = True
        else:
            continue
        new_min, new_max = _scaled_country_spawn_range(old_min, old_max, float(settings["scale"]))
        count_indent = re.match(r"[ \t]*", count_text).group(0)
        if scalar and new_min == new_max:
            replacement = f"{count_indent}count = {new_min}"
        else:
            replacement = (
                f"{count_indent}count = {{ integer_range = {{ min = {new_min} max = {new_max} }} }}"
            )
        block = block[:count_span[0]] + replacement + block[count_span[1]:]

        probabilities = list(settings["cluster_probabilities"])
        if probabilities:
            marker = re.search(
                r"(?m)^([ \t]*)set_variable\s*=\s*state_to_cede\s*(?:#.*)?$",
                block,
            )
            if not marker:
                raise ValueError("Não encontrei state_to_cede em um laço de criação de países")
            line_end = block.find("\n", marker.end())
            if line_end == -1:
                line_end = marker.end()
            indent = marker.group(1)
            cluster = _country_cluster_growth(probabilities, indent)
            addition = (
                "\n"
                + indent
                + "# BWG COUNTRY SCALE: expandir somente por estados vizinhos do mesmo proprietário"
                + "\n"
                + cluster
            )
            block = block[:line_end] + addition + block[line_end:]

        operations.append((block_start, block_end, block))
        loop_stats.append((old_min, old_max, new_min, new_max))

    if not operations:
        raise ValueError("Nenhum laço procedural de criação de países foi encontrado")
    for start, end, replacement in sorted(operations, reverse=True):
        text = text[:start] + replacement + text[end:]
    if brace_balance(text) != 0:
        raise ValueError("Country-scale patch deixou 02_random_stuff.txt com chaves desequilibradas")
    write_text(fx, text, bom=True)

    old_min_total = sum(item[0] for item in loop_stats)
    old_max_total = sum(item[1] for item in loop_stats)
    new_min_total = sum(item[2] for item in loop_stats)
    new_max_total = sum(item[3] for item in loop_stats)
    report_lines = [
        "RANDOMISED WORLD — ESCALA TERRITORIAL DOS PAÍSES",
        f"Modo: {mode}",
        f"Descrição: {settings['label']}",
        f"Laços continentais ajustados: {len(loop_stats)}",
        f"Faixa teórica de novos países do mod-base: {old_min_total}–{old_max_total}",
        f"Faixa teórica após o ajuste: {new_min_total}–{new_max_total}",
        f"Máximo de estados tentados por novo país: {1 + len(settings['cluster_probabilities'])}",
        "",
        "Observação: os valores são faixas de tentativas do script. O resultado real",
        "depende dos estados elegíveis disponíveis durante a inicialização da campanha.",
    ]
    write_text(mod_root / "COUNTRY_SCALE_REPORT_PT-BR.txt", "\n".join(report_lines) + "\n")
    return {
        "mode": mode,
        "loops": len(loop_stats),
        "old_min": old_min_total,
        "old_max": old_max_total,
        "new_min": new_min_total,
        "new_max": new_max_total,
        "max_cluster": 1 + len(settings["cluster_probabilities"]),
        "label": settings["label"],
    }

def _local_frontier_colonization_block() -> str:
    return """    ###############################
    # LOCAL FRONTIER COLONIZATION
    # This is not overseas expansion: only countries bordering decentralized
    # territory receive the frontier-colonization law.
    ###############################
    every_country = {
        limit = {
            any_neighbouring_state = {
                owner = {
                    is_country_type = decentralized
                }
            }
            NOT = {
                any_scope_state = {
                    OR = {
                        is_in_geographic_region = geographic_region_north_china
                        is_in_geographic_region = geographic_region_south_china
                        is_in_geographic_region = geographic_region_manchuria_old
                    }
                }
            }
        }
        activate_law = law_type:law_frontier_colonization
        set_institution_investment_level = {
            institution = institution_colonial_affairs
            level = 1
        }
    }"""


def _controlled_overseas_block(mode: str) -> str:
    parts = [_local_frontier_colonization_block()]
    if mode == "none":
        parts.append("""    ###############################
    # OVERSEAS TERRITORIES DISABLED
    ###############################""")
        return "\n\n".join(parts)

    if mode == "rare_colonial":
        count_expr = "{ integer_range = { min = 1 max = 2 } }"
        cluster_second = (3, 2)
        cluster_third = (6, 1)
    elif mode == "few_colonial":
        count_expr = "{ integer_range = { min = 3 max = 5 } }"
        cluster_second = (2, 3)
        cluster_third = (4, 1)
    else:
        raise ValueError(f"Modo ultramarino inválido: {mode}")

    empty2, add2 = cluster_second
    empty3, add3 = cluster_third
    parts.append(f"""    ###############################
    # CONTROLLED OVERSEAS DOMAINS
    # Only a handful of sufficiently large coastal countries receive one
    # compact, unincorporated colonial domain. The anchor must be coastal,
    # decentralized and not land-adjacent to the colonial power.
    ###############################
    while = {{
        count = {count_expr}
        random_country = {{
            limit = {{
                has_variable = ceded_states
                is_subject = no
                NOT = {{ is_country_type = decentralized }}
                num_states >= 4
                techs_researched > 18
                any_scope_state = {{ is_coastal = yes }}
                NOT = {{ has_variable = bwg_colonial_candidate_used }}
            }}
            set_variable = bwg_colonial_candidate_used
            save_scope_as = bwg_colonial_power

            random_state = {{
                limit = {{
                    is_coastal = yes
                    owner = {{
                        is_country_type = decentralized
                        NOT = {{ has_variable = bwg_colony_source_claimed }}
                    }}
                    NOT = {{
                        any_neighbouring_state = {{
                            owner = {{ scope:bwg_colonial_power ?= this }}
                        }}
                    }}
                }}
                owner = {{
                    save_scope_as = bwg_colony_source
                    set_variable = bwg_colony_source_claimed
                }}
                set_state_owner = scope:bwg_colonial_power
                set_state_type = unincorporated

                scope:bwg_colonial_power = {{
                    set_variable = bwg_has_overseas_domain
                    activate_law = law_type:law_colonial_exploitation
                    set_institution_investment_level = {{
                        institution = institution_colonial_affairs
                        level = 1
                    }}
                }}

                random_list = {{
                    {empty2} = {{}}
                    {add2} = {{
                        random_neighbouring_state = {{
                            limit = {{
                                owner = {{ scope:bwg_colony_source ?= this }}
                            }}
                            set_state_owner = scope:bwg_colonial_power
                            set_state_type = unincorporated
                        }}
                    }}
                }}
                random_list = {{
                    {empty3} = {{}}
                    {add3} = {{
                        random_neighbouring_state = {{
                            limit = {{
                                owner = {{ scope:bwg_colony_source ?= this }}
                            }}
                            set_state_owner = scope:bwg_colonial_power
                            set_state_type = unincorporated
                        }}
                    }}
                }}
            }}
        }}
    }}

    every_country = {{
        remove_variable = bwg_colonial_candidate_used
        remove_variable = bwg_colony_source_claimed
        remove_variable = bwg_has_overseas_domain
    }}""")
    return "\n\n".join(parts)


def _controlled_subject_block(mode: str) -> str:
    if mode == "none":
        return """    ###############################
    # PRE-EXISTING SUBJECTS DISABLED
    ###############################"""
    if mode == "very_rare":
        count_expr = "{ integer_range = { min = 0 max = 2 } }"
        subject_max = 1
        overlord_min = 6
    elif mode == "rare":
        count_expr = "{ integer_range = { min = 1 max = 4 } }"
        subject_max = 2
        overlord_min = 5
    else:
        raise ValueError(f"Modo de súditos inválido: {mode}")

    return f"""    ###############################
    # CONTROLLED PRE-EXISTING SUBJECTS
    # Globally capped. Subjects must be tiny, independent neighbours of a
    # substantially larger country, and their primary cultures must differ.
    # Controlled modes create puppets only; no random personal unions.
    ###############################
    while = {{
        count = {count_expr}
        random_country = {{
            limit = {{
                has_variable = ceded_states
                is_subject = no
                NOT = {{ is_country_type = decentralized }}
                num_states <= {subject_max}
                NOT = {{ has_variable = bwg_subject_candidate_used }}
                NOT = {{ has_variable = bwg_has_generated_subject }}
                any_scope_state = {{
                    any_neighbouring_state = {{
                        owner = {{
                            has_variable = ceded_states
                            is_subject = no
                            NOT = {{ is_country_type = decentralized }}
                            num_states >= {overlord_min}
                            NOT = {{ has_variable = bwg_has_generated_subject }}
                        }}
                    }}
                }}
            }}

            set_variable = bwg_subject_candidate_used
            save_scope_as = bwg_generated_subject
            random_primary_culture = {{
                save_scope_as = bwg_generated_subject_culture
            }}

            random_scope_state = {{
                limit = {{
                    any_neighbouring_state = {{
                        owner = {{
                            has_variable = ceded_states
                            is_subject = no
                            NOT = {{ is_country_type = decentralized }}
                            num_states >= {overlord_min}
                            NOT = {{ has_variable = bwg_has_generated_subject }}
                            NOT = {{
                                any_primary_culture = {{
                                    scope:bwg_generated_subject_culture ?= this
                                }}
                            }}
                        }}
                    }}
                }}
                random_neighbouring_state = {{
                    limit = {{
                        owner = {{
                            has_variable = ceded_states
                            is_subject = no
                            NOT = {{ is_country_type = decentralized }}
                            num_states >= {overlord_min}
                            NOT = {{ has_variable = bwg_has_generated_subject }}
                            NOT = {{
                                any_primary_culture = {{
                                    scope:bwg_generated_subject_culture ?= this
                                }}
                            }}
                        }}
                    }}
                    owner = {{ save_scope_as = bwg_generated_overlord }}
                }}
            }}

            if = {{
                limit = {{ exists = scope:bwg_generated_overlord }}
                scope:bwg_generated_overlord = {{
                    create_diplomatic_pact = {{
                        country = scope:bwg_generated_subject
                        type = puppet
                    }}
                    set_variable = bwg_has_generated_subject
                }}
                set_variable = bwg_is_generated_subject
            }}
        }}
    }}

    every_country = {{
        remove_variable = bwg_subject_candidate_used
        remove_variable = bwg_has_generated_subject
        remove_variable = bwg_is_generated_subject
    }}"""


SUBJECT_CREATION_ANCHOR = "    # BWG_BEFORE_SUBJECT_CREATION"


def _find_subject_section_start(text: str) -> int:
    """Locate the beginning of the subject-generation section.

    The section label changes when controlled subject modes replace the original
    randomizer block. Older generator versions searched only for the original
    label, which caused generation to fail whenever the recommended controlled
    mode was selected. This locator accepts all generated variants and has token
    fallbacks for templates whose comments were edited.
    """
    labels = (
        "# CONTROLLED PRE-EXISTING SUBJECTS",
        "# PRE-EXISTING SUBJECTS DISABLED",
        "# PRE-EXISTING SUBJECTS",
    )
    positions = [text.find(label) for label in labels]
    positions = [pos for pos in positions if pos >= 0]
    if positions:
        marker_pos = min(positions)
        separator = text.rfind("    ###############################", 0, marker_pos)
        return separator if separator >= 0 else text.rfind("\n", 0, marker_pos) + 1

    # Fallbacks for modified templates where comments were translated/renamed.
    for token in ("bwg_subject_candidate_used", "randomiser_current_puppet_subject"):
        token_pos = text.find(token)
        if token_pos >= 0:
            separator = text.rfind("    ###############################", 0, token_pos)
            if separator >= 0:
                return separator
    return -1


def patch_world_layout_effect(mod_root: Path, overseas_mode: str, subjects_mode: str) -> None:
    """Replace the template's mass-colonial and mass-subject generation.

    The original randomizer deliberately created many scattered overseas states
    and many subject relationships. Controlled modes keep only compact colonial
    clusters and a tiny global number of neighbour-based puppets.
    """
    fx = mod_root / 'common/scripted_effects/02_random_stuff.txt'
    text = read_text(fx)

    if overseas_mode != 'original':
        claim_anchor = text.index('state_region = {\n                add_claim = scope:country')
        colonial_start = text.index('\n    while = {\n        count = 5\n        every_country = {', claim_anchor)
        colonial_end_marker = '\n\tevery_country = {\n\t\tevery_scope_state = {\n\t\t\tlimit = {\n\t\t\t\tis_incorporated = yes'
        colonial_end = text.index(colonial_end_marker, colonial_start)
        text = text[:colonial_start] + '\n' + _controlled_overseas_block(overseas_mode) + '\n' + text[colonial_end:]

    if subjects_mode != 'original':
        subject_marker = text.index('# PRE-EXISTING SUBJECTS')
        subject_start = text.rfind('    ###############################', 0, subject_marker)
        subject_end_marker = '\n\tevery_country = {\n        limit = {\n            is_subject = no\n            NOT = { is_country_type = decentralized }'
        subject_end = text.index(subject_end_marker, subject_marker)
        text = text[:subject_start] + _controlled_subject_block(subjects_mode) + '\n' + text[subject_end:]

    # Leave a stable insertion point for cleanup passes that must run before
    # subjects are created. This is added after replacing the subject block so it
    # works with original, disabled, very-rare and rare subject modes alike.
    if SUBJECT_CREATION_ANCHOR not in text:
        subject_start = _find_subject_section_start(text)
        if subject_start < 0:
            raise ValueError(
                'Não encontrei a seção de criação de súditos para criar o ponto de limpeza territorial.'
            )
        text = text[:subject_start] + SUBJECT_CREATION_ANCHOR + '\n' + text[subject_start:]

    if brace_balance(text) != 0:
        raise ValueError('World-layout patch left 02_random_stuff.txt with unbalanced braces')
    write_text(fx, text, bom=True)


@dataclass
class HistoricalRemnantPlan:
    mode: str
    country_cores: Dict[str, List[str]]
    original_states: Dict[str, List[str]]
    capital_states: Dict[str, str]
    warnings: List[str] = field(default_factory=list)

    @property
    def audited_countries(self) -> int:
        return len(self.country_cores)


def _parse_initial_country_states(base: Path) -> Dict[str, List[str]]:
    '''Read 1836 ownership from history/states, including split states.'''
    history_dir = base / 'common/history/states'
    result: Dict[str, Set[str]] = defaultdict(set)
    if not history_dir.exists():
        return {}
    state_rx = re.compile(r'(?m)^\s*s:(STATE_[A-Z0-9_]+)\s*=\s*\{')
    for path in sorted(history_dir.glob('*.txt')):
        text = read_text(path)
        for match in state_rx.finditer(text):
            open_idx = text.find('{', match.start(), match.end())
            try:
                close_idx = matching_brace(text, open_idx)
            except ValueError:
                continue
            block = text[match.start():close_idx + 1]
            for tag in re.findall(r'\bcountry\s*=\s*c:([A-Z0-9_]+)', block):
                result[tag].add(match.group(1))
    return {tag: sorted(states) for tag, states in result.items()}


def _parse_country_capitals(base: Path) -> Dict[str, str]:
    '''Read country-definition capital states when available.'''
    result: Dict[str, str] = {}
    candidate_dirs = [
        base / 'common/country_definitions',
        base / 'common/country/country_definitions',
    ]
    block_rx = re.compile(r'(?m)^\s*(?:(?:REPLACE_OR_CREATE|INJECT_OR_CREATE):)?([A-Z][A-Z0-9_]{2,})\s*=\s*\{')
    for directory in candidate_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob('*.txt')):
            if path.name.endswith('.md'):
                continue
            text = read_text(path)
            for match in block_rx.finditer(text):
                open_idx = text.find('{', match.start(), match.end())
                try:
                    close_idx = matching_brace(text, open_idx)
                except ValueError:
                    continue
                block = text[match.start():close_idx + 1]
                cap = re.search(r'\bcapital\s*=\s*\"?(STATE_[A-Z0-9_]+)\"?', block)
                if cap:
                    result[match.group(1)] = cap.group(1)
    return result


def _connected_components(members: Iterable[str], adjacency: Dict[str, Set[str]]) -> List[Set[str]]:
    remaining = set(members)
    components: List[Set[str]] = []
    while remaining:
        start = min(remaining)
        stack = [start]
        component: Set[str] = set()
        while stack:
            state = stack.pop()
            if state in component or state not in remaining:
                continue
            component.add(state)
            for neighbour in adjacency.get(state, set()):
                if neighbour in remaining and neighbour not in component:
                    stack.append(neighbour)
        remaining.difference_update(component)
        components.append(component)
    return components


def build_historical_remnant_plan(base: Path, adjacency: Dict[str, Set[str]], mode: str) -> HistoricalRemnantPlan:
    original_states = _parse_initial_country_states(base)
    capitals = _parse_country_capitals(base)
    cores: Dict[str, List[str]] = {}
    warnings: List[str] = []
    for tag, owned in sorted(original_states.items()):
        if not owned:
            continue
        components = _connected_components(owned, adjacency)
        capital = capitals.get(tag, '')
        capital_component = next((c for c in components if capital in c), None) if capital else None
        if capital_component is None:
            # Without a definition capital, only disconnected multi-state tags
            # are safe to audit. Legitimate one-state island countries are kept.
            if len(components) <= 1:
                continue
            components.sort(key=lambda c: (-len(c), min(c)))
            capital_component = components[0]
            warnings.append(f'{tag}: capital não encontrada; maior componente inicial usado como núcleo.')
        # Every tag with a known territorial nucleus is audited.  v6.10 fixes a case where
        # fully connected countries such as France, so they could later survive
        # only in Corsica.  Keeping the original core here fixes France, Portugal,
        # Britain and equivalent displaced historical tags.
        cores[tag] = sorted(capital_component)
    return HistoricalRemnantPlan(mode, cores, original_states, capitals, warnings)

def _remnant_single_state_successor_block(color: Tuple[int, int, int]) -> str:
    '''Create one local procedural successor from the currently scoped state.'''
    return f'''                save_scope_as = bwg_remnant_state
                random_scope_culture = {{ save_scope_as = bwg_remnant_culture }}
                random_scope_pop = {{ religion = {{ save_scope_as = bwg_remnant_religion }} }}
                set_variable = bwg_historical_remnant_state
                scope:bwg_remnant_origin = {{
                    create_dynamic_country = {{
                        origin = scope:bwg_remnant_origin
                        culture = scope:bwg_remnant_culture
                        tier = principality
                        religion = scope:bwg_remnant_religion
                        cede_state_trigger = {{ has_variable = bwg_historical_remnant_state }}
                        on_created = {{
                            set_variable = ceded_states
                            set_variable = bwg_is_historical_successor
                            every_scope_state = {{ remove_variable = bwg_historical_remnant_state }}
                        }}
                        color = {{ {color[0]} {color[1]} {color[2]} }}
                    }}
                }}'''


def _remnant_localize_block(
    color: Tuple[int, int, int],
    iterations: int,
    absorb_any_procedural: bool,
) -> str:
    '''Resolve a remnant one state at a time, preventing a scattered successor.

    In dissolve mode, each state joins an adjacent procedural country when one
    exists. Isolated islands become their own local procedural country. In the
    procedural-only mode, states may join only an adjacent successor created by
    this cleanup system, keeping each connected cluster together.
    '''
    owner_condition = 'has_variable = ceded_states' if absorb_any_procedural else 'has_variable = bwg_is_historical_successor'
    successor = _remnant_single_state_successor_block(color)
    return f'''        while = {{
            count = {iterations}
            random_scope_state = {{
                if = {{
                    limit = {{
                        any_neighbouring_state = {{
                            owner = {{
                                {owner_condition}
                                NOT = {{ scope:bwg_remnant_origin ?= this }}
                                NOT = {{ is_country_type = decentralized }}
                            }}
                        }}
                    }}
                    random_neighbouring_state = {{
                        limit = {{
                            owner = {{
                                {owner_condition}
                                NOT = {{ scope:bwg_remnant_origin ?= this }}
                                NOT = {{ is_country_type = decentralized }}
                            }}
                        }}
                        owner = {{ save_scope_as = bwg_remnant_recipient }}
                    }}
                    set_state_owner = scope:bwg_remnant_recipient
                }}
                else = {{
{successor}
                }}
            }}
        }}'''

def write_historical_remnant_cleanup(
    base: Path,
    mod_root: Path,
    adjacency: Dict[str, Set[str]],
    mode: str,
    seed: int,
) -> HistoricalRemnantPlan:
    '''Remove historical tags that lost their original territorial nucleus.

    The core is the initial connected component containing the defined capital;
    when definitions are unavailable, the largest initial component is used.
    Legitimate island countries retain their tag as long as they still own part
    of that original core. Only countries stranded exclusively in disconnected
    peripheral possessions are replaced or absorbed.
    '''
    if mode == 'preserve':
        return HistoricalRemnantPlan(mode, {}, {}, {}, [])
    plan = build_historical_remnant_plan(base, adjacency, mode)
    if not plan.country_cores:
        return plan

    entries: List[str] = []
    for index, (tag, core_states) in enumerate(sorted(plan.country_cores.items())):
        local_rng = random.Random(f'bwg-v6.10-remnant:{seed}:{tag}')
        color = tuple(local_rng.randint(45, 210) for _ in range(3))
        core_trigger = '\n'.join(f'                    s:{state} = {{ owner = c:{tag} }}' for state in core_states)
        iterations = max(4, len(plan.original_states.get(tag, [])) + 4)
        procedural = _remnant_localize_block(color, iterations, absorb_any_procedural=False)
        dissolve = _remnant_localize_block(color, iterations, absorb_any_procedural=True)
        if mode == 'procedural':
            action = procedural
        elif mode == 'rare_exiles':
            action = f'''        random_list = {{
            1 = {{ set_variable = bwg_historical_exile_preserved }}
            7 = {{
{dissolve}
            }}
        }}'''
        else:
            action = dissolve
        entries.append(f'''    c:{tag} = {{
        if = {{
            limit = {{
                num_states > 0
                NOT = {{
                    OR = {{
{core_trigger}
                    }}
                }}
            }}
            save_scope_as = bwg_remnant_origin
{action}
        }}
    }}''')

    effect_text = 'bwg_cleanup_historical_remnants_effect = {\n' + '\n\n'.join(entries) + '''

    every_country = {
        remove_variable = bwg_historical_exile_preserved
    }
}
'''
    effect_path = mod_root / 'common/scripted_effects/98_historical_remnant_cleanup.txt'
    if brace_balance(effect_text) != 0:
        raise ValueError('Historical remnant effect has unbalanced braces')
    write_text(effect_path, effect_text, bom=True)

    main_fx = mod_root / 'common/scripted_effects/02_random_stuff.txt'
    text = read_text(main_fx)
    injection = '''    # Historical-tag cleanup: remove countries stranded only in former peripheral possessions.
    bwg_cleanup_historical_remnants_effect = yes

'''

    # The world-layout patch normally creates this stable anchor. Keep a fallback
    # locator so direct calls and older templates remain compatible.
    marker_pos = text.find(SUBJECT_CREATION_ANCHOR)
    if marker_pos < 0:
        marker_pos = _find_subject_section_start(text)
    if marker_pos < 0:
        raise ValueError(
            'Não encontrei a seção de criação de súditos para inserir a limpeza de remanescentes históricos.'
        )
    if 'bwg_cleanup_historical_remnants_effect = yes' not in text:
        text = text[:marker_pos] + injection + text[marker_pos:]
    if brace_balance(text) != 0:
        raise ValueError('Historical remnant injection unbalanced 02_random_stuff.txt')
    write_text(main_fx, text, bom=True)
    return plan

def _weighted_choice(rng: random.Random, items: Sequence[Tuple[str,int]]) -> str:
    total=sum(w for _,w in items)
    roll=rng.randrange(total)
    acc=0
    for value,w in items:
        acc+=w
        if roll<acc:
            return value
    return items[-1][0]


def write_country_profiles(base: Path, mod_root: Path, mode: str, seed: int) -> None:
    if mode=="keep":
        return
    src_dir=base/'common/history/countries'
    if not src_dir.exists():
        return
    for src in sorted(src_dir.glob('*.txt')):
        text=read_text(src)
        text=re.sub(r"(?m)^[ \t]*effect_starting_technology_tier_[1-7]_tech[ \t]*=[ \t]*yes[ \t]*(?:#.*)?\r?\n","",text)
        text=re.sub(r"(?m)^[ \t]*add_technology_researched[ \t]*=[ \t]*[a-zA-Z0-9_]+[ \t]*(?:#.*)?\r?\n","",text)
        profile_lines=[]
        if mode=="equalized":
            profile_lines=["\t\teffect_starting_technology_tier_3_tech = yes"]
        else:
            local_rng=random.Random(f"bwg-v6.1:{seed}:{src.name}")
            # In the base game lower tier numbers are more technologically advanced.
            tier=int(_weighted_choice(local_rng,[("2",8),("3",24),("4",48),("5",20)]))
            politics_by_tier={
                2:[("liberal",42),("conservative",48),("traditional",10)],
                3:[("conservative",50),("liberal",22),("traditional",23),("princely_state",5)],
                4:[("traditional",48),("conservative",37),("princely_state",10),("reactionary",5)],
                5:[("traditional",55),("reactionary",25),("princely_state",15),("conservative",5)],
            }
            politics=_weighted_choice(local_rng,politics_by_tier[tier])
            text=re.sub(r"(?m)^[ \t]*effect_starting_politics_[a-zA-Z0-9_]+[ \t]*=[ \t]*yes[ \t]*(?:#.*)?\r?\n","",text)
            # Historical tag-specific laws would reintroduce Europe/Asia biases after a natural profile is selected.
            text=re.sub(r"(?m)^[ \t]*activate_law[ \t]*=[ \t]*law_type:[a-zA-Z0-9_]+[ \t]*(?:#.*)?\r?\n","",text)
            profile_lines=[
                f"\t\teffect_starting_technology_tier_{tier}_tech = yes",
                f"\t\teffect_starting_politics_{politics} = yes",
            ]
        insertion="\n".join(profile_lines)
        text=re.sub(r"(?m)^(\s*c:[A-Z0-9_]+\s*\?=\s*\{\s*)$",lambda m:m.group(1)+"\n"+insertion,text)
        write_text(mod_root/src.relative_to(base),text,bom=True)



def patch_automatic_foreign_investment(base: Path, mod_root: Path, mode: str) -> int:
    """Disable autonomous AI foreign investment in generated worlds.

    Explicit player actions and treaty mechanics remain available.  Only the AI
    weighting on building groups is overridden, which prevents countries from
    acquiring isolated mines/factories abroad during the opening months merely
    because the generated markets happen to make them attractive.
    """
    if mode != 'block_ai':
        return 0
    source_dir = base / 'common/building_groups'
    if not source_dir.exists():
        return 0
    changed = 0
    for source in sorted(source_dir.glob('*.txt')):
        target = mod_root / source.relative_to(base)
        text = read_text(target if target.exists() else source)
        text, count = re.subn(
            r'(?m)^([ \t]*foreign_investment_ai_factor[ \t]*=[ \t]*)[-+0-9.]+([ \t]*(?:#.*)?)$',
            r'\g<1>0\2',
            text,
        )
        if count:
            if brace_balance(text) != 0:
                raise ValueError(f'Override de investimento estrangeiro desequilibrou {source.name}')
            write_text(target, text, bom=True)
            changed += count
    return changed


def strip_historical_foreign_investment_pacts(base: Path, mod_root: Path) -> int:
    """Remove only historical diplomatic pacts whose type concerns investment.

    This is intentionally narrower than the optional full diplomacy reset.
    """
    source_dir = base / 'common/history/countries'
    if not source_dir.exists():
        return 0
    removed = 0
    for source in sorted(source_dir.glob('*.txt')):
        target = mod_root / source.relative_to(base)
        text = read_text(target if target.exists() else source)
        cursor = 0
        pieces=[]
        dirty=False
        while True:
            match = re.search(r'(?m)^[ \t]*create_diplomatic_pact\s*=\s*\{', text[cursor:])
            if not match:
                pieces.append(text[cursor:])
                break
            start = cursor + match.start()
            op = text.find('{', start, cursor + match.end())
            end = matching_brace(text, op) + 1
            block = text[start:end]
            pieces.append(text[cursor:start])
            if re.search(r'(?i)\binvest(?:ment|ment_rights|ment_agreement)?\b', block):
                while end < len(text) and text[end] in '\r\n':
                    end += 1
                removed += 1
                dirty=True
            else:
                pieces.append(block)
            cursor=end
        if dirty:
            out=''.join(pieces)
            if brace_balance(out) != 0:
                raise ValueError(f'Remoção de pacto de investimento desequilibrou {source.name}')
            write_text(target,out,bom=True)
    return removed




def _strip_named_effect_blocks(text: str, names: Sequence[str]) -> Tuple[str,int]:
    """Remove scripted effect blocks/assignments by exact key."""
    removed=0
    name_alt='|'.join(re.escape(name) for name in names)
    while True:
        match=re.search(rf'(?m)^[ \t]*(?:{name_alt})[ \t]*=[ \t]*\{{',text)
        if not match:
            break
        op=text.find('{',match.start(),match.end())
        end=matching_brace(text,op)+1
        while end<len(text) and text[end] in '\r\n':
            end+=1
        text=text[:match.start()]+text[end:]
        removed+=1
    text,count=re.subn(rf'(?m)^[ \t]*(?:{name_alt})[ \t]*=[ \t]*[^\n{{}}]+\r?\n','',text)
    return text,removed+count



def strip_initial_building_history(base: Path, mod_root: Path) -> Dict[str,int]:
    """Neutralize vanilla/DLC starting building history before procedural rebuilding.

    Some installations apply regional building history late in initialization.
    Without overriding these files, England can regain historical shipyards after
    the balanced pass.  This cleanup is global and contains no regional exception.
    """
    history_root=base/'common/history'
    stats={'files_cleared':0,'blocks_removed':0}
    if not history_root.exists():
        return stats
    building_dirs={'buildings','building','state_buildings','building_history'}
    cleared=set()
    for directory in history_root.rglob('*'):
        if not directory.is_dir() or directory.name.lower() not in building_dirs:
            continue
        for source in directory.rglob('*.txt'):
            target=mod_root/source.relative_to(base)
            write_text(target,'# Starting building history removed by Randomised World v6.15.\n',bom=True)
            cleared.add(source.resolve())
            stats['files_cleared']+=1
    tokens=('create_building','add_building','set_building_level','create_state_building','add_state_building')
    for source in history_root.rglob('*.txt'):
        if source.resolve() in cleared or source.name.endswith('.md'):
            continue
        target=mod_root/source.relative_to(base)
        text=read_text(target if target.exists() else source)
        new_text,count=_strip_named_effect_blocks(text,tokens)
        if count:
            if brace_balance(new_text)!=0:
                raise ValueError(f'Remoção de histórico de construções desequilibrou {source.relative_to(base)}')
            write_text(target,new_text,bom=True)
            stats['blocks_removed']+=count
    return stats

def strip_initial_military_history(base: Path, mod_root: Path) -> Dict[str,int]:
    """Remove all vanilla starting formations and units.

    Dynamic borders make historical armies and navies fiscally nonsensical. A
    country inheriting Britain's old formations could spend tens of thousands
    per week despite having no military buildings. The generator now clears
    formation-history files and strips equivalent creation blocks wherever the
    installed game version stores them.
    """
    history_root=base/'common/history'
    stats={'files_cleared':0,'blocks_removed':0}
    if not history_root.exists():
        return stats
    military_dir_names={'military_formations','military_formation','armies','navies','military_units'}
    cleared=set()
    for directory in history_root.rglob('*'):
        if not directory.is_dir() or directory.name.lower() not in military_dir_names:
            continue
        for source in directory.rglob('*.txt'):
            target=mod_root/source.relative_to(base)
            write_text(target,'# Starting military formations removed by Randomised World v6.15.\n',bom=True)
            cleared.add(source.resolve())
            stats['files_cleared']+=1
    tokens=(
        'create_military_formation','create_military_unit','create_army','create_navy',
        'create_ship','add_battalion','add_flotilla','add_military_unit','add_unit',
    )
    for source in history_root.rglob('*.txt'):
        if source.resolve() in cleared or source.name.endswith('.md'):
            continue
        target=mod_root/source.relative_to(base)
        text=read_text(target if target.exists() else source)
        new_text,count=_strip_named_effect_blocks(text,tokens)
        if count:
            if brace_balance(new_text)!=0:
                raise ValueError(f'Remoção militar desequilibrou {source.relative_to(base)}')
            write_text(target,new_text,bom=True)
            stats['blocks_removed']+=count
    return stats


def strip_historical_power_blocs(base: Path, mod_root: Path) -> Dict[str,int]:
    """Clear inherited power blocs/treaties in procedural diplomacy modes."""
    history_root=base/'common/history'
    stats={'files_cleared':0,'blocks_removed':0}
    if not history_root.exists():
        return stats
    dir_tokens=('power_bloc','power_blok','treaties','diplomatic_pacts')
    cleared=set()
    for directory in history_root.rglob('*'):
        if not directory.is_dir() or not any(tok in directory.name.lower() for tok in dir_tokens):
            continue
        for source in directory.rglob('*.txt'):
            target=mod_root/source.relative_to(base)
            write_text(target,'# Historical geopolitical setup removed by Randomised World v6.15.\n',bom=True)
            cleared.add(source.resolve())
            stats['files_cleared']+=1
    tokens=('create_power_bloc','add_to_power_bloc','join_power_bloc','set_power_bloc','create_treaty')
    for source in history_root.rglob('*.txt'):
        if source.resolve() in cleared or source.name.endswith('.md'):
            continue
        target=mod_root/source.relative_to(base)
        text=read_text(target if target.exists() else source)
        new_text,count=_strip_named_effect_blocks(text,tokens)
        if count:
            if brace_balance(new_text)!=0:
                raise ValueError(f'Remoção de bloco de poder desequilibrou {source.relative_to(base)}')
            write_text(target,new_text,bom=True)
            stats['blocks_removed']+=count
    return stats


def patch_subsistence_infrastructure(base: Path, mod_root: Path, usage: float=0.2) -> int:
    """Make unused arable land locally supplied instead of consuming one full infrastructure per level.

    Auto-placed subsistence buildings inherit the agriculture group's value of
    1 infrastructure per level in the current data. After arable-land
    randomization this alone produced states at 110/41 infrastructure before any
    real industry existed. A low explicit child-group value preserves some local
    transport pressure while preventing automatic subsistence land from
    destroying market access at game start.
    """
    source_dir=base/'common/building_groups'
    changed=0
    if not source_dir.exists():
        return changed
    for source in sorted(source_dir.glob('*.txt')):
        target=mod_root/source.relative_to(base)
        text=read_text(target if target.exists() else source)
        ops=[]
        for _name,start,op,cl,end in top_level_blocks(text,r'[a-z][a-z0-9_]+'):
            block=text[start:end]
            if not re.search(r'(?m)^\s*is_subsistence\s*=\s*yes\b',block):
                continue
            m=re.search(r'(?m)^(\s*)infrastructure_usage_per_level\s*=\s*[-+0-9.]+',block)
            if m:
                a=start+m.start(); b=start+m.end()
                replacement=f'{m.group(1)}infrastructure_usage_per_level = {usage:g}'
            else:
                marker=re.search(r'(?m)^(\s*)is_subsistence\s*=\s*yes\s*$',block)
                if not marker:
                    continue
                a=start+marker.end(); b=a
                replacement=f'\n{marker.group(1)}infrastructure_usage_per_level = {usage:g}'
            ops.append((a,b,replacement)); changed+=1
        for a,b,replacement in sorted(ops,reverse=True):
            text=text[:a]+replacement+text[b:]
        if ops:
            if brace_balance(text)!=0:
                raise ValueError(f'Ajuste de infraestrutura de subsistência desequilibrou {source.name}')
            write_text(target,text,bom=True)
    return changed

def patch_localization(mod_root: Path) -> None:
    replacements={
        'english':{
            'rule_randomiser_remove_all_buildings':'"Initial Buildings"',
            'setting_randomiser_remove_buildings':'"True Empty Start"',
            'setting_randomiser_remove_buildings_desc':'"Removes economic, government, military, infrastructure and ownership buildings. Automatic buildings may be recreated by the game engine."',
            'setting_randomiser_randomise_buildings':'"Balanced Random Buildings"',
            'setting_randomiser_randomise_buildings_desc':'"Removes existing buildings, creates a conservative local economy, and then grants small armies, navies and shipyards only to countries with sufficient population, technology, coastline and domestic supply chains."',
            'setting_randomiser_keep_buildings':'"Keep Vanilla Buildings"',
            'setting_randomiser_keep_buildings_desc':'"Keeps the original starting buildings."',
        },
        'braz_por':{
            'rule_randomiser_remove_all_buildings':'"Construções iniciais"',
            'setting_randomiser_remove_buildings':'"Início realmente vazio"',
            'setting_randomiser_remove_buildings_desc':'"Remove construções econômicas, públicas, militares, de infraestrutura e de propriedade. Construções automáticas podem ser recriadas pelo motor do jogo."',
            'setting_randomiser_randomise_buildings':'"Construções aleatórias balanceadas"',
            'setting_randomiser_randomise_buildings_desc':'"Remove as construções existentes, cria uma economia local conservadora e só então concede pequenos exércitos, marinhas e estaleiros a países com população, tecnologia, litoral e cadeias domésticas suficientes."',
            'setting_randomiser_keep_buildings':'"Manter construções originais"',
            'setting_randomiser_keep_buildings_desc':'"Mantém as construções iniciais do jogo-base."',
        }
    }
    for lang,vals in replacements.items():
        p=mod_root/f'localization/{lang}/extra_countries_l_{lang}.yml'
        if not p.exists(): continue
        t=read_text(p)
        for key,val in vals.items():
            rx=re.compile(rf"(?m)^(\s*{re.escape(key)}\s*:)\s*.*$")
            if rx.search(t): t=rx.sub(rf"\1 {val}",t)
            else: t += f"\n {key}: {val}\n"
        write_text(p,t,bom=True)


def copy_template(template_dir: Path, out_root: Path) -> None:
    if out_root.exists(): shutil.rmtree(out_root)
    shutil.copytree(template_dir,out_root)


def update_metadata(mod_root: Path, seed: int) -> None:
    p=mod_root/'.metadata/metadata.json'
    meta=json.loads(read_text(p))
    meta['name']=f'[1.13] Randomised World — World History Generator v6.15 (Seed {seed})'
    meta['id']=f'waddlerandomworld_history_v6_15_{seed}'
    meta['version']='6.14.0'
    meta['short_description']='Procedural world generation with a playable civil economy, initial trade centres, merchant marine, civilian shipbuilding and economically limited armed forces.'
    write_text(p,json.dumps(meta,ensure_ascii=False,indent=2)+"\n")


def validate(states: Dict[str,StateData], original_totals: dict, options: dict, mod_root: Path) -> List[str]:
    msgs=[]
    if options['arable_land']!='keep':
        orig=original_totals['arable']; new=sum(s.new_arable_land for s in states.values())
        if orig!=new: raise ValueError(f'Arable total mismatch {orig} != {new}')
        msgs.append(f'Terra arável: {orig:,} -> {new:,}')
    if options['arable_resources']!='keep':
        orig_ar=original_totals['arable_resources']
        new_ar=Counter(r for s in states.values() for r in s.new_arable_resources)
        if orig_ar!=new_ar:
            raise ValueError(f'Arable resource availability mismatch: {orig_ar-new_ar}')
        msgs.append(f'Produtos agrícolas preservados exatamente: {sum(orig_ar.values()):,} disponibilidades')
    if options['resources']!='keep':
        orig=original_totals['resources_by_building']; now=Counter()
        for s in states.values():
            for b,n in s.new_capped.items(): now[b]+=n
            for r in s.new_resources: now[r.building]+=r.total_amount
        if orig!=now:
            diff=orig-now; raise ValueError(f'Resource total mismatch: {diff}')
        max_total=max((len(s.new_capped)+len(s.new_resources) for s in states.values()),default=0)
        max_strategic=max((
            sum(1 for b in s.new_capped if b in STRATEGIC_RESOURCE_BUILDINGS)
            + sum(1 for r in s.new_resources if r.building in STRATEGIC_RESOURCE_BUILDINGS)
            for s in states.values()
        ),default=0)
        if max_total>6 or max_strategic>4:
            raise ValueError(f'Resource stacking exceeded: total={max_total}, strategic={max_strategic}')
        for s in states.values():
            for r in s.new_resources:
                if r.building in {"building_oil_rig","building_rubber_plantation"} and r.discovered_amount:
                    raise ValueError(f'Advanced resource starts discovered in {s.name}: {r.building}')
        _geo, gradual=resource_mode_parts(options['resources'])
        if gradual:
            visible=Counter(); hidden=Counter()
            for s in states.values():
                for b,n in s.new_capped.items(): visible[b]+=n
                for r in s.new_resources:
                    visible[r.building]+=r.discovered_amount
                    hidden[r.building]+=r.amount if r.amount_key=='undiscovered_amount' else 0
            for b in GRADUAL_MINERAL_BUILDINGS:
                if orig.get(b,0)>0 and hidden[b]<=0:
                    raise ValueError(f'No hidden reserve generated for {b}')
            group_file=mod_root/'common/building_groups/00_building_groups.txt'
            if not group_file.exists() or not re.search(r'bg_mining\s*=\s*\{[\s\S]*?discoverable_resource\s*=\s*yes',read_text(group_file)):
                raise ValueError('Native discovery was not enabled for bg_mining')
            msgs.append(f'Recursos preservados: {len(orig)} tipos; em 1836, minerais básicos visíveis={sum(visible[b] for b in GRADUAL_MINERAL_BUILDINGS):,}, reservas ocultas={sum(hidden[b] for b in GRADUAL_MINERAL_BUILDINGS):,}')
        else:
            msgs.append(f'Recursos preservados exatamente: {len(orig)} tipos/categorias; máximo de {max_strategic} recursos estratégicos por estado')
    if options['population']!='keep':
        orig=original_totals['population']; new=sum(s.target_population for s in states.values())
        if orig!=new: raise ValueError(f'Population total mismatch {orig} != {new}')
        msgs.append(f'População: {orig:,} -> {new:,}')
    if options.get('buildings')=='balanced':
        generated=read_text(mod_root/'common/scripted_effects/99_balanced_world_generated_buildings.txt')
        if 'every_country = {' not in generated or 'every_scope_state = {' not in generated:
            raise ValueError('Playable-economy civil-building loop was not generated')
        if 'name = bwg_country_population' in generated:
            raise ValueError('Legacy country-population aggregate returned to the civil-building pass')
        if generated.count('create_building = {') < 30:
            raise ValueError('Civil-building generator contains too few creation branches')
        required_tech_floor=('enclosure','manufacturies','shaft_mining','urbanization','tech_bureaucracy')
        if any(f'add_technology_researched = {tech}' not in generated for tech in required_tech_floor):
            raise ValueError('Minimum economic technology floor is incomplete')
        required_civil_buildings=(
            'building_wheat_farm','building_livestock_ranch','building_logging_camp',
            'building_iron_mine','building_coal_mine','building_food_industry',
            'building_tooling_workshop','building_construction_sector',
            'building_government_administration','building_trade_center','building_port','building_shipyard'
        )
        if any(b not in generated for b in required_civil_buildings):
            raise ValueError('Playable civil-economy branches are incomplete')
        required_commerce_tokens=(
            'pm_basic_port','pm_trade_center','pm_trade_center_trade_quantity_limited',
            'pm_basic_shipbuilding','name = bwg_trade_level','name = bwg_port_level'
        )
        if any(token not in generated for token in required_commerce_tokens):
            raise ValueError('Initial trade and merchant-marine layer is incomplete')
        if 'building_naval_administration' in generated or 'building_barrack' in generated:
            raise ValueError('Military-only buildings leaked into the civil economy pass')
        if re.search(r'geographic_region_|STATE_HOME_COUNTIES|STATE_WEST_COUNTRY',generated,re.I):
            raise ValueError('Civil economy contains a historical regional bias')
        patched_effect=read_text(mod_root/'common/scripted_effects/02_random_stuff.txt')
        if options.get('buildings')=='balanced':
            block_start=patched_effect.index('randomise_buildings_effect = {')
            block_open=patched_effect.index('{',block_start)
            block=patched_effect[block_start:matching_brace(patched_effect,block_open)+1]
            if 'balanced_world_generate_buildings = yes' not in block or 'has_game_rule' in block:
                raise ValueError('Balanced economy still depends on the lobby game-rule branch')
        for b in ENGINE_MANAGED_BUILDINGS:
            if re.search(rf'building\s*=\s*"?{re.escape(b)}"?',generated):
                raise ValueError(f'Engine-managed building generated directly: {b}')
        source_fx=read_text(mod_root/'common/scripted_effects/02_random_stuff.txt')
        if re.search(r'(?m)^[ \t]*create_building\s*=\s*\{',source_fx):
            raise ValueError('The original randomizer still contains unconditional create_building effects')
        uncommented=re.sub(r'(?m)#.*$','',source_fx)
        if re.search(r'\b(create_ship|create_military_formation)\s*=\s*\{',uncommented):
            raise ValueError('Legacy fleet generation remains active in the source randomizer')
        military_file=mod_root/'common/scripted_effects/99_balanced_world_generated_military.txt'
        if not military_file.exists():
            raise ValueError('Economic military generator was not written')
        military_text=read_text(military_file)
        selected_mode=options.get('military_economy','economic_conservative')
        if selected_mode=='none':
            if re.search(r'\b(create_ship|create_military_formation|building_shipyard|building_naval_administration|building_barrack)\b',military_text):
                raise ValueError('Military-disabled mode still generates forces')
        else:
            required=('bwg_army_level','bwg_navy_level','country_has_building_type_levels','is_incorporated = yes','save_scope_as = bwg_military_country')
            if any(marker not in military_text for marker in required):
                raise ValueError('Economic aptitude safeguards missing from military generator')
            if 'is_in_geographic_region = geographic_region_england_old' in military_text:
                raise ValueError('Military generator contains a fixed England bonus')
            if re.search(r'building_shipyard\"?\s+level\s*=\s*scope:bwg_(?:army|navy)_level',military_text):
                raise ValueError('Shipyard level is incorrectly tied directly to force size')
        if re.search(r'\badd_ownership\s*=\s*\{',re.sub(r'(?m)#.*$','',generated)):
            raise ValueError('Generated buildings contain explicit ownership blocks')
        if 'is_incorporated = yes' not in generated or 'owner = scope:bwg_country' not in generated:
            raise ValueError('Local incorporated-state safeguards missing from generated buildings')
        msgs.append('Construções: piso econômico funcional, extração mineral visível e núcleo industrial/público no capital; somente em estados incorporados do próprio país')
        mode_label=MILITARY_ECONOMY_PROFILES.get(options.get('military_economy','economic_conservative'),{}).get('label',options.get('military_economy'))
        msgs.append('Forças iniciais: '+str(mode_label)+'; exército, marinha e estaleiros dependem de população, tecnologia, litoral e cadeias domésticas')
    if options.get('historical_remnants') != 'preserve':
        remnant_fx = mod_root / 'common/scripted_effects/98_historical_remnant_cleanup.txt'
        main_fx = mod_root / 'common/scripted_effects/02_random_stuff.txt'
        if not remnant_fx.exists():
            raise ValueError('Historical-remnant cleanup effect was not generated')
        remnant_text = read_text(remnant_fx)
        if 'bwg_cleanup_historical_remnants_effect' not in remnant_text:
            raise ValueError('Historical-remnant cleanup effect is incomplete')
        if 'bwg_cleanup_historical_remnants_effect = yes' not in read_text(main_fx):
            raise ValueError('Historical-remnant cleanup is not called before subject generation')
        on_actions = mod_root / 'common/on_actions/01_random_stuff.txt'
        if not on_actions.exists() or 'bwg_cleanup_historical_remnants_effect = yes' not in read_text(on_actions):
            raise ValueError('Historical-remnant cleanup is not called again before the building reset')
        if re.search(r'(?m)^\s*c:[A-Z0-9_]+\s*\?=', remnant_text):
            raise ValueError('Historical-remnant cleanup contains invalid optional country scopes')
        msgs.append('Remanescentes históricos: auditoria de núcleo territorial antes dos súditos e nova limpeza antes da recriação dos edifícios')
    if options.get('foreign_investment') == 'block_ai':
        group_dir = mod_root / 'common/building_groups'
        remaining=[]
        for group_file in group_dir.glob('*.txt') if group_dir.exists() else []:
            for match in re.finditer(r'(?m)^[ \t]*foreign_investment_ai_factor[ \t]*=[ \t]*([-+0-9.]+)', read_text(group_file)):
                if float(match.group(1)) != 0.0:
                    remaining.append((group_file.name,match.group(1)))
        if remaining:
            raise ValueError(f'foreign_investment_ai_factor remained active: {remaining[:3]}')
        msgs.append('Propriedade extraterritorial: investimento estrangeiro automático da IA neutralizado; apenas ações/tratados explícitos podem criá-lo')
    if options.get('buildings')=='balanced':
        group_dir=mod_root/'common/building_groups'
        subsistence_values=[]
        for group_file in group_dir.glob('*.txt') if group_dir.exists() else []:
            gt=read_text(group_file)
            for _name,start,_op,_cl,end in top_level_blocks(gt,r'[a-z][a-z0-9_]+'):
                block=gt[start:end]
                if re.search(r'(?m)^\s*is_subsistence\s*=\s*yes\b',block):
                    m=re.search(r'(?m)^\s*infrastructure_usage_per_level\s*=\s*([-+0-9.]+)',block)
                    subsistence_values.append(float(m.group(1)) if m else 1.0)
        if not subsistence_values or max(subsistence_values)>0.25:
            raise ValueError(f'Infraestrutura de subsistência não foi limitada: {subsistence_values}')
        msgs.append('Infraestrutura: edifícios de subsistência usam 0,2 por nível; terra arável automática não pode mais criar congestionamento extremo sozinha')
    history_root=mod_root/'common/history'
    bad_building_history=[]
    if history_root.exists():
        for hp in history_root.rglob('*.txt'):
            clean=re.sub(r'(?m)#.*$','',read_text(hp))
            if re.search(r'\b(create_building|add_building|set_building_level|create_state_building|add_state_building)\s*=',clean):
                bad_building_history.append(str(hp.relative_to(mod_root)))
    if bad_building_history:
        raise ValueError(f'Histórico regional de construções ainda ativo: {bad_building_history[:3]}')
    msgs.append('Histórico de construções: removido antes da economia procedural; nenhum estaleiro regional histórico pode reaparecer')
    bad_military=[]
    if history_root.exists():
        for hp in history_root.rglob('*.txt'):
            clean=re.sub(r'(?m)#.*$','',read_text(hp))
            if re.search(r'\b(create_military_formation|create_military_unit|create_army|create_navy|create_ship|add_battalion|add_flotilla)\s*=',clean):
                bad_military.append(str(hp.relative_to(mod_root)))
    if bad_military:
        raise ValueError(f'Histórico militar inicial ainda ativo: {bad_military[:3]}')
    msgs.append('Forças históricas removidas: nenhum custo militar herdado de tags antigas; apenas forças recalculadas pelo módulo econômico podem ser criadas')
    if options.get('technology') in {'equalized','natural_spread'}:
        country_dir=mod_root/'common/history/countries'
        bad=[]; tiers=Counter(); politics=Counter()
        for cp in country_dir.glob('*.txt'):
            ct=read_text(cp)
            if re.search(r'(?m)^[ \t]*add_technology_researched\s*=',ct):
                bad.append(cp.name); continue
            found_tiers=re.findall(r'effect_starting_technology_tier_([1-7])_tech',ct)
            if not found_tiers:
                bad.append(cp.name); continue
            for x in found_tiers: tiers[x]+=1
            for x in re.findall(r'effect_starting_politics_([a-zA-Z0-9_]+)',ct): politics[x]+=1
            if options.get('technology')=='equalized' and any(x!='3' for x in found_tiers):
                bad.append(cp.name)
            if options.get('technology')=='natural_spread' and re.search(r'(?m)^[ \t]*activate_law\s*=',ct):
                bad.append(cp.name)
        if bad: raise ValueError(f'Country profile normalization failed: {bad[:3]}')
        if options.get('technology')=='equalized':
            msgs.append('Tecnologia: históricos nacionais equalizados no tier 3; avanços extras removidos')
        else:
            msgs.append(f'Perfis nacionais globais: tiers {dict(sorted(tiers.items()))}; política {dict(politics.most_common())}')
    for p in mod_root.rglob('*.txt'):
        txt=read_text(p)
        if brace_balance(txt)!=0: raise ValueError(f'Unbalanced braces: {p}')
    return msgs


def generate_report(mod_root: Path, states: Dict[str,StateData], options: dict, seed: int, validation: List[str]) -> None:
    lines=[
        'RANDOMISED WORLD — WORLD HISTORY GENERATOR v6.15',
        f'Seed: {seed}',
        '',
        'OPTIONS',
        json.dumps(options,ensure_ascii=False,indent=2),
        '',
        'VALIDATION',
        *validation,
        '',
        'NOTES',
        '- World totals for randomized population, arable land and resource potential are preserved.',
        '- Gradual mode exposes only a small 1836 share of minerals and stores the remainder as native undiscovered resources.',
        '- Population follows arable land, food access, coasts, state traits and visible resources; cultural-block mode moves complete POP compositions into contiguous zones.',
        '- Initial buildings use a food-first state budget, a country-sized consumer core, trade centres, basic ports that produce merchant marine and sparse civilian shipyards; historical military formations are replaced only after an economic-aptitude test.',
        '- Economic military safety caps barracks, naval administration, shipyards and ships; no historical region receives a fixed bonus.',
        '- Automatic AI foreign investment can be disabled, preventing isolated foreign mines/factories from reappearing in the opening months.',
        '- Historical-remnant cleanup runs both before subject creation and again immediately before the building reset.',
        '- Five national economic archetypes create different shortages and trade incentives while preserving a small viability floor.',
    ]
    write_text(mod_root/'BALANCED_WORLD_REPORT.txt','\n'.join(lines)+"\n")




def write_military_economy_report(mod_root: Path, mode: str) -> None:
    profile=MILITARY_ECONOMY_PROFILES.get(mode,MILITARY_ECONOMY_PROFILES["economic_conservative"])
    army_pop={"economic_conservative":1_350_000,"economic_balanced":900_000,"economic_strong":550_000}.get(mode,0)
    navy_pop={"economic_conservative":1_600_000,"economic_balanced":1_050_000,"economic_strong":700_000}.get(mode,0)
    lines=[
        "FORÇAS MILITARES INICIAIS — AVALIAÇÃO ECONÔMICA COMPATÍVEL",
        "",
        f"Modo: {profile['label']}",
        "",
        "A economia civil é criada primeiro, pelo padrão país -> estado usado pelo randomizador-base.",
        "Nenhuma região histórica recebe bônus. A Inglaterra é avaliada pelas mesmas condições dos demais países.",
        "",
    ]
    if mode=='none':
        lines += ["Resultado: nenhum quartel, administração naval, exército ou frota militar é criado automaticamente. Estaleiros civis ainda podem existir para abastecer a Marinha Mercante."]
    else:
        lines += [
            f"Exército: o capital precisa sustentar aproximadamente 1 nível por {army_pop:,} habitantes, com teto {profile['army_cap']}.",
            "São exigidos alimentos processados, ferramentas e uma base de madeira, ferro ou território; Standing Army é concedida somente após essa aprovação econômica.",
            f"Marinha: um único estado costeiro precisa ter ao menos {navy_pop:,} habitantes, porto, madeira, ferramentas e tecido; Admiralty é concedida somente após essa aprovação econômica.",
            f"Administração naval: teto {profile['navy_cap']}; a avaliação militar não duplica estaleiros civis já existentes.",
            "Apenas fragatas são criadas automaticamente nesta reconstrução compatível.",
        ]
    lines += [
        "",
        "Limitação: preços, emprego e saldo semanal só são calculados pelo motor do Victoria 3 depois do carregamento.",
    ]
    write_text(mod_root/'AVALIACAO_MILITAR_INICIAL_PT-BR.txt','\n'.join(lines)+'\n')

def write_descriptors(mod_root: Path, output_parent: Path, seed: int) -> Path:
    name=f'[1.13] Randomised World — World History Generator v6.15 (Seed {seed})'
    inner='\n'.join([
        f'name="{name}"',
        'version="6.15.0"',
        'supported_version="1.13.*"',
        'tags={ "Gameplay" "Alternative History" "New Nations" }',
    ])+'\n'
    write_text(mod_root/'descriptor.mod',inner)
    outer=inner+f'path="mod/{mod_root.name}"\n'
    descriptor=output_parent/f'{mod_root.name}.mod'
    write_text(descriptor,outer)
    return descriptor


def zip_install_bundle(folder: Path, descriptor: Path, zip_path: Path) -> None:
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        z.write(descriptor,descriptor.name)
        for p in sorted(folder.rglob('*')):
            arc=Path(folder.name)/p.relative_to(folder)
            if p.is_dir(): z.writestr(str(arc).replace('\\','/')+'/',b'')
            else: z.write(p,str(arc).replace('\\','/'))

def zip_folder(folder: Path, zip_path: Path) -> None:
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in sorted(folder.rglob('*')):
            arc=Path(folder.name)/p.relative_to(folder)
            if p.is_dir(): z.writestr(str(arc).replace('\\','/')+'/',b'')
            else: z.write(p,str(arc).replace('\\','/'))


def generate_world(
    game_root: Path,
    template_dir: Path,
    output_parent: Path,
    options: dict,
    seed: int,
    preview_callback: Optional[Callable[[str], bool]] = None,
) -> Tuple[Path,Path,List[str]]:
    from world_history_modules import (
        apply_strategic_region_plan,
        build_exact_adjacency,
        make_strategic_region_plan,
        parse_state_provinces,
        parse_company_definitions,
        parse_diplomatic_action_keys,
        parse_historical_company_keys,
        select_civilization_centers,
        strip_historical_diplomacy,
        validate_world_history,
        write_strategic_region_overrides,
        write_world_history_report,
        write_world_history_scripts,
    )
    from country_color_modules import (
        apply_country_color_plan,
        build_country_color_plan,
        validate_country_color_plan,
    )
    from power_distribution_modules import (
        apply_power_distribution_plan,
        build_power_distribution_plan,
        build_seed_preview_report,
        validate_power_distribution,
    )

    game_root=game_root.resolve()
    if (game_root/'game/map_data/state_regions').exists():
        base=game_root/'game'
    elif (game_root/'map_data/state_regions').exists():
        base=game_root
    else:
        raise FileNotFoundError('Pasta do jogo inválida: não encontrei game/map_data/state_regions.')
    state_dir=base/'map_data/state_regions'
    pop_dir=base/'common/history/pops'
    sr_dir=base/'common/strategic_regions'
    for required in (state_dir,pop_dir,sr_dir):
        if not required.exists():
            raise FileNotFoundError(f'Pasta necessária ausente: {required}')

    options={
        'resources':options.get('resources','plausible_gradual'),
        'resource_visibility':options.get('resource_visibility','sparse'),
        'arable_land':options.get('arable_land','global'),
        'arable_resources':options.get('arable_resources','natural'),
        'population':options.get('population','global'),
        'cultures':options.get('cultures','natural_blocks'),
        'buildings':options.get('buildings','balanced'),
        'fiscal_safety':options.get('fiscal_safety','strict'),
        'military_economy':options.get('military_economy','economic_conservative'),
        'technology':options.get('technology','natural_spread'),
        'development':options.get('development','normal'),
        'intensity':options.get('intensity','medium'),
        'strategic_regions':options.get('strategic_regions','contiguous'),
        'country_colors':options.get('country_colors','neighbour_contrast'),
        'country_scale':options.get('country_scale','balanced'),
        'power_distribution':options.get('power_distribution','balanced_continents'),
        'companies':options.get('companies','natural_dynamic'),
        'strategic_needs':options.get('strategic_needs','natural'),
        'diplomacy':options.get('diplomacy','natural_relations'),
        'civilization_centers':options.get('civilization_centers','sparse'),
        'overseas_territories':options.get('overseas_territories','rare_colonial'),
        'subjects':options.get('subjects','very_rare'),
        'historical_remnants':options.get('historical_remnants','dissolve'),
        'foreign_investment':options.get('foreign_investment','block_ai'),
    }
    rng=random.Random(seed)
    states,file_texts,file_blocks=parse_state_regions(state_dir)
    building_keys=parse_building_keys(base/'common/buildings')
    building_unlocks=parse_building_unlocks(base/'common/buildings')
    strategic=parse_strategic_regions(sr_dir)
    region_orders,region_sequence=parse_strategic_region_order(sr_dir)
    for s in states.values():
        s.strategic_region=strategic.get(s.name,'region_unassigned')
        order=region_orders.get(s.strategic_region,[])
        s.region_order=order.index(s.name) if s.name in order else 9999
        s.climate_profile=classify_climate(s)

    pop_blocks,pop_texts=parse_population(pop_dir)
    for name,pb in pop_blocks.items():
        if name in states:
            states[name].population=pb.total
            states[name].target_population=pb.total

    # Strategic regions are established before economic and cultural allocation, so
    # the rest of the generator treats the procedural geography as the actual world.
    strategic_plan=make_strategic_region_plan(
        state_dir,
        sr_dir,
        base/'common/strait_definitions',
        base/'map_data/provinces.png',
        states,
        options['strategic_regions'],
        random.Random(f'bwg-v6.1-regions:{seed}'),
    )
    region_orders,region_sequence=apply_strategic_region_plan(states,strategic_plan)
    country_color_plan=build_country_color_plan(
        base,
        states,
        strategic_plan,
        options['country_colors'],
        seed,
    )

    original_totals={
        'arable':sum(s.arable_land for s in states.values()),
        'population':sum(pb.total for pb in pop_blocks.values() if pb.state in states),
        'resources':Counter(),
        'resources_by_building':Counter(),
        'arable_resources':Counter(r for s in states.values() for r in s.arable_resources),
        'cultures':Counter(),
    }
    for pb in pop_blocks.values():
        for e in pb.entries:
            original_totals['cultures'][e.culture]+=e.size
    for s in states.values():
        for b,n in s.capped.items():
            original_totals['resources'][("capped",b)]+=n
            original_totals['resources_by_building'][b]+=n
        for r in s.resources:
            original_totals['resources'][("resource",r.building)]+=r.total_amount
            original_totals['resources_by_building'][r.building]+=r.total_amount

    if options['arable_land']!='keep':
        randomize_arable_land(states,rng,options['arable_land'],options['intensity'])
    if options['arable_resources']!='keep':
        randomize_arable_resources(states,rng,options['arable_resources'],options['intensity'])
    if options['resources']!='keep':
        randomize_resources(states,rng,options['resources'],options['intensity'],options['resource_visibility'])
    if options['population']!='keep':
        randomize_population(states,pop_blocks,rng,options['population'],options['intensity'])

    homelands: Dict[str,List[str]]={}
    if options['cultures']=='natural_blocks':
        homelands=assign_cultural_blocks(states,pop_blocks,region_sequence,rng,options['intensity'])

    centers=select_civilization_centers(
        states,
        options['civilization_centers'],
        random.Random(f'bwg-v6.1-centers:{seed}'),
    )
    power_plan=build_power_distribution_plan(
        base,
        states,
        strategic_plan,
        centers,
        options['power_distribution'],
        seed,
    )
    company_definitions,company_warnings=parse_company_definitions(base)
    historical_company_keys=parse_historical_company_keys(base)
    diplomatic_actions=parse_diplomatic_action_keys(base)

    mod_name=f'Randomised_World_History_v6_15_{seed}'
    staging_parent=output_parent/f'.bwg_staging_v6_15_{os.getpid()}_{seed}'
    if staging_parent.exists():
        shutil.rmtree(staging_parent)
    staging_parent.mkdir(parents=True,exist_ok=True)
    mod_root=staging_parent/mod_name
    copy_template(template_dir,mod_root)
    update_metadata(mod_root,seed)
    foreign_investment_patches=0
    historical_investment_pacts_removed=0
    military_history_cleanup={'files_cleared':0,'blocks_removed':0}
    building_history_cleanup={'files_cleared':0,'blocks_removed':0}
    power_bloc_cleanup={'files_cleared':0,'blocks_removed':0}
    subsistence_groups_patched=0
    color_fallbacks,color_definitions=apply_country_color_plan(country_color_plan,mod_root)
    country_scale_stats=patch_country_scale_effect(mod_root, options['country_scale'])
    patch_world_layout_effect(mod_root, options['overseas_territories'], options['subjects'])
    remnant_adjacency = strategic_plan.adjacency
    if options['historical_remnants'] != 'preserve' and not any(remnant_adjacency.values()):
        province_map = base / 'map_data/provinces.png'
        if not province_map.exists():
            raise FileNotFoundError(
                'Para limpar remanescentes históricos sem apagar países insulares legítimos, '
                f'o gerador precisa de {province_map}. Selecione a instalação completa do Victoria 3.'
            )
        state_provinces, _macro_order = parse_state_provinces(state_dir)
        remnant_adjacency, _centroids, _map_size = build_exact_adjacency(
            states.keys(), state_provinces, base / 'common/strait_definitions', province_map
        )
    remnant_plan = write_historical_remnant_cleanup(
        base,
        mod_root,
        remnant_adjacency,
        options['historical_remnants'],
        seed,
    )
    _resource_geo,gradual_resources=resource_mode_parts(options['resources'])
    patch_building_effect(mod_root,options['buildings'],options['development'],building_keys,building_unlocks,gradual_resources,options['fiscal_safety'],options['military_economy'])
    write_discoverable_mining_override(base,mod_root,gradual_resources)
    write_country_profiles(base,mod_root,options['technology'],seed)
    military_history_cleanup=strip_initial_military_history(base,mod_root)
    building_history_cleanup=strip_initial_building_history(base,mod_root)
    if options['buildings']=='balanced':
        subsistence_groups_patched=patch_subsistence_infrastructure(base,mod_root,0.2)
    foreign_investment_patches=patch_automatic_foreign_investment(
        base, mod_root, options['foreign_investment']
    )
    if options['foreign_investment']=='block_ai':
        historical_investment_pacts_removed=strip_historical_foreign_investment_pacts(base,mod_root)
    historical_diplomacy_removed=0
    if options['diplomacy']!='keep':
        historical_diplomacy_removed=strip_historical_diplomacy(base,mod_root)
        power_bloc_cleanup=strip_historical_power_blocs(base,mod_root)
    patch_localization(mod_root)

    if options['strategic_regions']!='keep':
        write_strategic_region_overrides(strategic_plan,base,mod_root)
    if options['arable_land']!='keep' or options['arable_resources']!='keep' or options['resources']!='keep':
        write_state_region_overrides(states,file_texts,file_blocks,base,mod_root,options)
    if options['population']!='keep' or options['cultures']=='natural_blocks':
        write_population_overrides_natural(
            states,pop_blocks,pop_texts,base,mod_root,
            cultural_blocks=options['cultures']=='natural_blocks',
        )
    if homelands:
        write_homeland_overrides(base,mod_root,homelands)

    module_warnings=write_world_history_scripts(
        base,
        mod_root,
        options,
        centers,
        company_definitions,
        historical_company_keys,
        diplomatic_actions,
    )
    apply_power_distribution_plan(mod_root,power_plan)
    all_warnings=list(strategic_plan.warnings)+company_warnings+module_warnings+list(power_plan.warnings)

    validation=validate(states,original_totals,options,mod_root)
    validation.extend(validate_country_color_plan(country_color_plan,mod_root))
    validation.extend(validate_world_history(states,strategic_plan,centers,mod_root,options))
    validation.extend(validate_power_distribution(mod_root,power_plan))
    if options['cultures']=='natural_blocks':
        validation.append(f'Culturas: {len(set(pb.dominant_culture for pb in pop_blocks.values() if pb.dominant_culture))} culturas dominantes redistribuídas em blocos; homelands reescritas em {len(homelands)} estados')
    geology_states=sum(bool(s.geology_belts) for s in states.values())
    validation.append(f'Geologia: cinturões minerais em {geology_states} estados, agrupados pelas regiões estratégicas procedurais')
    validation.append('Economias: cinco arquétipos nacionais com necessidades estruturais e companhias condicionadas à economia real')
    overseas_labels = {
        'none': 'nenhum território ultramarino procedural',
        'rare_colonial': '1–2 potências coloniais globais, com domínios costeiros compactos de 1–3 estados',
        'few_colonial': '3–5 potências coloniais globais, com domínios costeiros compactos de 1–3 estados',
        'original': 'lógica ultramarina original do mod-base',
    }
    subject_labels = {
        'none': 'nenhum súdito procedural',
        'very_rare': '0–2 vassalos globais; apenas países de 1 estado junto a vizinho de 6+ estados',
        'rare': '1–4 vassalos globais; apenas países de até 2 estados junto a vizinho de 5+ estados',
        'original': 'lógica de súditos original do mod-base',
    }
    cluster_unit = "estado contíguo" if country_scale_stats['max_cluster'] == 1 else "estados contíguos"
    validation.append(
        'Escala dos países: ' + country_scale_stats['label']
        + f"; tentativas continentais {country_scale_stats['new_min']}–{country_scale_stats['new_max']}"
        + f"; até {country_scale_stats['max_cluster']} {cluster_unit} por novo país"
    )
    validation.append('Ultramar: ' + overseas_labels[options['overseas_territories']])
    validation.append('Histórico militar removido: ' + str(military_history_cleanup['files_cleared']) + ' arquivos de formações limpos e ' + str(military_history_cleanup['blocks_removed']) + ' blocos removidos')
    validation.append('Histórico regional de construções removido: ' + str(building_history_cleanup['files_cleared']) + ' arquivos limpos e ' + str(building_history_cleanup['blocks_removed']) + ' blocos removidos; Inglaterra não recebe estaleiros históricos')
    validation.append('Forças iniciais recalculadas: ' + MILITARY_ECONOMY_PROFILES[options['military_economy']]['label'] + '; sem bônus geográfico fixo')
    validation.append('Infraestrutura de subsistência: ' + str(subsistence_groups_patched) + ' grupos limitados a 0,2 por nível')
    if options['diplomacy']!='keep':
        validation.append('Blocos de poder históricos removidos: ' + str(power_bloc_cleanup['files_cleared']) + ' arquivos e ' + str(power_bloc_cleanup['blocks_removed']) + ' blocos')
    validation.append('Súditos: ' + subject_labels[options['subjects']])
    remnant_labels = {
        'preserve': 'todos os remanescentes históricos preservados',
        'dissolve': 'tags históricas sem seu núcleo original são absorvidas por vizinhos procedurais ou substituídas localmente',
        'procedural': 'tags históricas sem seu núcleo original viram países procedurais locais',
        'rare_exiles': 'aproximadamente 1 em 8 remanescentes pode sobreviver como governo histórico no exílio',
    }
    validation.append(
        'Remanescentes históricos: ' + remnant_labels[options['historical_remnants']]
        + f'; {remnant_plan.audited_countries} países com possessões iniciais desconectadas auditados'
    )
    validation.extend(f'AVISO DE NÚCLEO: {warning}' for warning in remnant_plan.warnings)
    if country_color_plan is not None:
        validation.append(f'Cores auxiliares: {color_fallbacks} fallbacks dinâmicos e {color_definitions} definições nacionais recoloridas')
    if historical_diplomacy_removed:
        validation.append(f'Diplomacia histórica neutralizada: {historical_diplomacy_removed} pactos/relações removidos dos históricos nacionais')
    if options['foreign_investment']=='block_ai':
        validation.append(
            f'Investimento estrangeiro automático da IA: bloqueado em {foreign_investment_patches} ponderações de grupos de construção; ações e tratados explícitos continuam disponíveis'
        )
        if historical_investment_pacts_removed:
            validation.append(f'Pactos históricos de investimento removidos: {historical_investment_pacts_removed}')
    else:
        validation.append('Investimento estrangeiro automático da IA: regras originais preservadas')
    validation.extend(f'AVISO: {warning}' for warning in all_warnings)

    generate_report(mod_root,states,options,seed,validation)
    write_military_economy_report(mod_root,options['military_economy'])
    write_world_history_report(
        mod_root,states,options,seed,strategic_plan,centers,
        len(company_definitions),len(diplomatic_actions),all_warnings,
    )
    preview_text=build_seed_preview_report(
        states,options,seed,strategic_plan,centers,power_plan,
        country_scale_stats,all_warnings,validation,
    )
    write_text(mod_root/'PANORAMA_DA_SEED_PT-BR.txt',preview_text)
    if preview_callback is not None and not preview_callback(preview_text):
        shutil.rmtree(staging_parent,ignore_errors=True)
        raise GenerationCancelled('A seed foi descartada antes de salvar o mod.')

    final_mod_root=output_parent/mod_name
    if final_mod_root.exists():
        shutil.rmtree(final_mod_root)
    shutil.move(str(mod_root),str(final_mod_root))
    shutil.rmtree(staging_parent,ignore_errors=True)
    descriptor=write_descriptors(final_mod_root,output_parent,seed)
    out_zip=output_parent/f'{mod_name}_INSTALL.zip'
    zip_install_bundle(final_mod_root,descriptor,out_zip)
    return final_mod_root,out_zip,validation

