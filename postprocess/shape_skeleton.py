#!/usr/bin/env python3
"""shape_skeleton — compute common-backbone (LCS) across all invocations
of one callback and visualise the conditional graph.

Hypothesis: for a callback that branches on input, all invocations
share a common subsequence of CPU API calls (the "skeleton"); each
variant adds extra inserted calls (the "branches"). The LCS over all
invocations recovers the skeleton; the residue per invocation is its
branch payload.

Outputs:
  1. dag_out/skeleton_F<id>.txt — printable summary (skeleton sequence,
     per-invocation alignment, per-class skeleton coverage statistics).
  2. dag_out/skeleton_F<id>.svg / .png — conditional graph: skeleton
     drawn as a backbone, each unique branch pattern shown as an
     insertion edge labelled with the count + class colour.

Usage:
  python3 shape_skeleton.py <session> <F#> [-o dag_out]
                            [--target cpu|all]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shape_phase_classifier import (_extract_invocations, _per_sig_stats,
                                    _classify)


CLASS_COLORS = {
    "dominant": "#2E7D32",   # green
    "anomaly":  "#D32F2F",   # red
    "shutdown": "#7B1FA2",   # purple
    "init":     "#1976D2",   # blue
    "variant":  "#9E9E9E",   # gray
}


# ---------------------------------------------------------------------------
# LCS
# ---------------------------------------------------------------------------

def lcs(a, b):
    """Standard O(mn) LCS for sequences of arbitrary hashable tokens.
    Returns the LCS as a list of tokens.
    """
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m):
        for j in range(n):
            if a[i] == b[j]:
                dp[i+1][j+1] = dp[i][j] + 1
            else:
                dp[i+1][j+1] = max(dp[i+1][j], dp[i][j+1])
    out = []
    i, j = m, n
    while i > 0 and j > 0:
        if a[i-1] == b[j-1]:
            out.append(a[i-1]); i -= 1; j -= 1
        elif dp[i-1][j] >= dp[i][j-1]:
            i -= 1
        else:
            j -= 1
    return list(reversed(out))


def lcs_multi(seqs):
    """Iteratively intersect: start with shortest seq, LCS with each of
    the rest. Result is a sequence common to all (one valid answer when
    multiple LCS have equal length)."""
    if not seqs:
        return []
    order = sorted(range(len(seqs)), key=lambda i: len(seqs[i]))
    skeleton = list(seqs[order[0]])
    for k in order[1:]:
        if not skeleton:
            return []
        skeleton = lcs(skeleton, seqs[k])
    return skeleton


def _contains_substring(seq, sub):
    """Check if sub appears as a contiguous block in seq."""
    L = len(sub)
    if L == 0: return True
    if L > len(seq): return False
    for i in range(len(seq) - L + 1):
        ok = True
        for j in range(L):
            if seq[i+j] != sub[j]:
                ok = False
                break
        if ok:
            return True
    return False


def lcsubstr_multi(seqs):
    """Longest contiguous block (substring) common to ALL sequences.
    Enumerates substrings of the shortest sequence in decreasing length
    and returns the first one found in all others.
    """
    if not seqs:
        return []
    shortest = min(seqs, key=len)
    L = len(shortest)
    for length in range(L, 0, -1):
        for start in range(L - length + 1):
            sub = list(shortest[start:start+length])
            if all(_contains_substring(s, sub) for s in seqs):
                return sub
    return []


def align_to_skeleton(seq, skeleton):
    """Return list of length len(seq) where each entry is True if that
    position in seq is matched to a skeleton token (in order), False if
    it is an insertion. Greedy left-to-right alignment of skeleton onto
    seq."""
    flags = [False] * len(seq)
    j = 0  # skeleton index
    for i, tok in enumerate(seq):
        if j < len(skeleton) and tok == skeleton[j]:
            flags[i] = True
            j += 1
    return flags


def insertions_between_skeleton(seq, flags):
    """Yield (skeleton_idx_before, [tokens]) tuples for each insertion
    block. skeleton_idx_before = -1 means before-first; otherwise the
    skeleton position immediately before the insertion."""
    blocks = []
    cur = []
    skel_pos = -1
    for tok, f in zip(seq, flags):
        if f:
            if cur:
                blocks.append((skel_pos, cur))
                cur = []
            skel_pos += 1
        else:
            cur.append(tok)
    if cur:
        blocks.append((skel_pos, cur))
    return blocks


def find_substring_in_subseq(subseq, substring):
    """Return start index of substring as a CONTIGUOUS block within
    subseq, else -1. The LCS substring (contiguous in invocations) is
    necessarily ALSO a subsequence of any invocation, including the LCS
    subseq when treated as a sequence."""
    L = len(substring)
    if L == 0 or L > len(subseq):
        return -1
    for i in range(len(subseq) - L + 1):
        if subseq[i:i+L] == substring:
            return i
    return -1


# ---------------------------------------------------------------------------
# Trie of insertions at a skeleton position
# ---------------------------------------------------------------------------

class TrieNode:
    __slots__ = ("token", "count", "ends_here", "children")

    def __init__(self, token=None):
        self.token = token
        self.count = 0      # invocations passing through this node
        self.ends_here = 0  # invocations whose insertion ends exactly here
        self.children = {}


def build_position_trie(blocks_at_pos):
    """blocks_at_pos: list of (tuple_of_tokens, count). Build trie."""
    root = TrieNode()
    for tokens, cnt in blocks_at_pos:
        cur = root
        for t in tokens:
            if t not in cur.children:
                cur.children[t] = TrieNode(token=t)
            cur = cur.children[t]
            cur.count += cnt
        cur.ends_here += cnt
    return root


# ---------------------------------------------------------------------------
# CPU-name extraction
# ---------------------------------------------------------------------------

def cpu_names(dag, mode="cpu"):
    """Extract token sequence for an invocation.
       mode='cpu' → CPU node names (control-flow skeleton)
       mode='all' → CPU + SM + CE in idx-order, with SM/CE prefixed
    """
    if mode == "cpu":
        return [n["name"] for n in dag["nodes"] if n["pe"] == "CPU"]
    seq = []
    for n in dag["nodes"]:
        if n["pe"] == "CPU":
            seq.append("CPU:" + n["name"])
        elif n["pe"] == "SM":
            seq.append("SM:" + n["name"][:20])
        elif n["pe"] == "CE":
            seq.append("CE:" + n["name"])
    return seq


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

SHORTEN = lambda s: s.replace("cudaLaunchKernel_v7000", "cudaLK") \
                     .replace("cudaMemcpyAsync_v3020", "cudaMC") \
                     .replace("cudaMemsetAsync_v3020", "cudaMS") \
                     .replace("cudaMemcpy_v3020", "cudaMCs") \
                     .replace("cuLaunchKernelEx", "cuLKEx") \
                     .replace("cuLaunchKernel", "cuLK") \
                     .replace("cudaStreamSynchronize_v3020", "cudaSync") \
                     .replace("cudaStreamIsCapturing_v10000", "isCap") \
                     .replace("memcpy_H2D", "MC_H2D") \
                     .replace("memcpy_D2H", "MC_D2H") \
                     .replace("memcpy_", "MC_") \
                     .replace("_v3020", "")[:18]


def render_label(token):
    """Produce display label for a token. Tokens may be raw API names
    (when target=cpu — implicit CPU) or "CPU:name"/"SM:name"/"CE:name"
    when target=all. Always prefix [PE] so the figure shows where each
    op runs."""
    if token.startswith("CPU:"):
        return f"[CPU] {SHORTEN(token[4:])}"
    if token.startswith("SM:"):
        return f"[SM] {SHORTEN(token[3:])}"
    if token.startswith("CE:"):
        return f"[CE] {SHORTEN(token[3:])}"
    # Default: target=cpu, all tokens are CPU
    return f"[CPU] {SHORTEN(token)}"


# ---------------------------------------------------------------------------
# Legend (shared across figures)
# ---------------------------------------------------------------------------

def _legend_dot():
    """Return a DOT cluster that explains node colours, edge types,
    pseudo-nodes, and the abbreviation table used in skeleton figures."""
    return '''
  subgraph cluster_legend {
    label="Legend"; labelloc=t; labeljust=l;
    fontname="Helvetica-Bold"; fontsize=10;
    style="filled,rounded"; fillcolor="#FAFAFA"; color="#999";
    node [fontname="Helvetica", fontsize=8];

    // Node colour swatches
    lg_sk    [label="skeleton (LCS subseq)", shape=box,
              style="filled,rounded", fillcolor="#BBDEFB"];
    lg_sub   [label="LCS substring portion", shape=box,
              style="filled,rounded", fillcolor="#FFD54F"];
    lg_br    [label="branch token (insertion)", shape=box,
              style="filled,rounded", fillcolor="#FFE0B2"];
    lg_pseudo [label="START / END (pseudo, not a CUDA call)",
               shape=note, fillcolor="#E1F5FE", style=filled];

    // Class colours (used in fig1/fig2 collapsed boxes)
    lg_dom  [label="dominant", shape=box, style="filled,rounded",
             fillcolor="#2E7D32", fontcolor="white"];
    lg_anom [label="anomaly",  shape=box, style="filled,rounded",
             fillcolor="#D32F2F", fontcolor="white"];
    lg_var  [label="variant",  shape=box, style="filled,rounded",
             fillcolor="#9E9E9E", fontcolor="white"];

    // Edge type swatches
    le_a [label="", shape=point, width=0.04];
    le_b [label="", shape=point, width=0.04];
    le_a -> le_b [label="skeleton (direct)", color="#1976D2", penwidth=2,
                  fontsize=8];
    le_c [label="", shape=point, width=0.04];
    le_d [label="", shape=point, width=0.04];
    le_c -> le_d [label="enter branch (abs %)", color="#888", style=dashed,
                  fontsize=8];
    le_e [label="", shape=point, width=0.04];
    le_f [label="", shape=point, width=0.04];
    le_e -> le_f [label="continue (cond %)", fontsize=8];
    le_g [label="", shape=point, width=0.04];
    le_h [label="", shape=point, width=0.04];
    le_g -> le_h [label="end branch (cond %)", color="#666", style=dashed,
                  fontsize=8];

    // CUDA API abbreviation table
    lg_abbrev [shape=note, fillcolor="#FFFFFF", style=filled,
               label=<<table border="0" cellborder="0" cellpadding="1">
                 <tr><td colspan="2"><b>API abbreviations</b></td></tr>
                 <tr><td align="right">cudaLK</td><td align="left">cudaLaunchKernel</td></tr>
                 <tr><td align="right">cudaMC</td><td align="left">cudaMemcpyAsync</td></tr>
                 <tr><td align="right">cudaMCs</td><td align="left">cudaMemcpy (sync)</td></tr>
                 <tr><td align="right">cudaMS</td><td align="left">cudaMemsetAsync</td></tr>
                 <tr><td align="right">cuLK</td><td align="left">cuLaunchKernel (driver)</td></tr>
                 <tr><td align="right">cuLKEx</td><td align="left">cuLaunchKernelEx (driver)</td></tr>
                 <tr><td align="right">cudaSync</td><td align="left">cudaStreamSynchronize</td></tr>
                 <tr><td align="right">MC_H2D</td><td align="left">memcpy host→device (CE)</td></tr>
                 <tr><td align="right">MC_D2H</td><td align="left">memcpy device→host (CE)</td></tr>
               </table>>];

    // PE prefix table
    lg_pe [shape=note, fillcolor="#FFFFFF", style=filled,
           label=<<table border="0" cellborder="0" cellpadding="1">
             <tr><td colspan="2"><b>Node label format</b></td></tr>
             <tr><td align="right">[CPU]</td><td align="left">host-side API call</td></tr>
             <tr><td align="right">[SM]</td><td align="left">GPU streaming-multiprocessor (kernel)</td></tr>
             <tr><td align="right">[CE]</td><td align="left">GPU copy engine (memcpy/memset)</td></tr>
           </table>>];

    // Layout — invisible edges to stack legend items
    lg_sk -> lg_sub [style=invis];
    lg_sub -> lg_br [style=invis];
    lg_br -> lg_pseudo [style=invis];
    lg_pseudo -> lg_dom [style=invis];
    lg_dom -> lg_anom [style=invis];
    lg_anom -> lg_var [style=invis];
  }
'''


# ---------------------------------------------------------------------------
# Figure 2: subseq skeleton with LCS-substring portion highlighted
# ---------------------------------------------------------------------------

def render_skeleton_with_substring(skeleton, substring, per_inv, classes,
                                    out_base, title):
    """Same backbone as figure 1 but highlight the LCS-substring portion
    of the LCS-subsequence skeleton in gold. Branches collapsed (top 15)
    as in figure 1."""
    sub_start = find_substring_in_subseq(skeleton, substring)
    sub_end = sub_start + len(substring) if sub_start >= 0 else -1

    block_counts = Counter()
    block_class = defaultdict(Counter)
    for sig, inv_list in per_inv.items():
        cls = classes.get(sig, "variant")
        seq = inv_list[0]["cpu_seq"]
        flags = align_to_skeleton(seq, skeleton)
        for skel_idx, tokens in insertions_between_skeleton(seq, flags):
            key = (skel_idx, tuple(tokens))
            block_counts[key] += len(inv_list)
            block_class[key][cls] += len(inv_list)

    out = ["digraph skeleton_dual {",
           '  rankdir=LR;',
           '  node [shape=box, style="filled,rounded", fontname="Helvetica",'
           ' fontsize=8, margin="0.05,0.02"];',
           '  edge [fontname="Helvetica", fontsize=7];',
           f'  label="{title}"; labelloc=t; labeljust=l; fontsize=10;',
           '  graph [nodesep=0.15, ranksep=0.25];',
           '',
           '  subgraph cluster_skel {',
           '    label="LCS subseq (light blue) · LCS substring (gold)";',
           '    style="filled,rounded"; fillcolor="#F0F7FF";',
           '    fontname="Helvetica-Bold"; fontsize=9;',
           ]

    show_max = 25
    n_skel = len(skeleton)
    show_idxs = list(range(min(show_max, n_skel)))
    if n_skel > show_max:
        show_idxs = list(range(12)) + list(range(n_skel - 8, n_skel))

    for idx in show_idxs:
        in_sub = (sub_start <= idx < sub_end)
        fill = "#FFD54F" if in_sub else "#BBDEFB"
        out.append(f'    sk{idx} [label="{idx}\\n{render_label(skeleton[idx])}",'
                   f' fillcolor="{fill}"];')

    prev = None
    for idx in show_idxs:
        if prev is not None:
            in_sub_edge = (sub_start <= prev and idx == prev + 1
                           and idx < sub_end)
            ecol = "#F57C00" if in_sub_edge else "#1976D2"
            ew = 3 if in_sub_edge else 2
            if idx == prev + 1:
                out.append(f'    sk{prev} -> sk{idx} [color="{ecol}", penwidth={ew}];')
            else:
                out.append(f'    sk{prev} -> sk{idx} [color="{ecol}", '
                           f'penwidth={ew}, label="…(+{idx-prev-1})", style=dashed];')
        prev = idx
    out.append('  }')

    blocks_sorted = block_counts.most_common(15)
    for bi, ((skel_idx, tokens), cnt) in enumerate(blocks_sorted):
        cls = block_class[(skel_idx, tokens)].most_common(1)[0][0]
        col = CLASS_COLORS.get(cls, "#999999")
        if len(tokens) <= 4:
            tok_str = "\\n".join(render_label(t) for t in tokens)
        else:
            tok_str = "\\n".join(render_label(t) for t in tokens[:3])
            tok_str += f"\\n+{len(tokens)-3} more"
        anchor_from = f"sk{skel_idx}" if skel_idx in show_idxs \
                      else f"sk{show_idxs[0]}"
        a2 = skel_idx + 1
        anchor_to = f"sk{a2}" if a2 in show_idxs else f"sk{show_idxs[-1]}"
        bid = f"br{bi}"
        out.append(f'  {bid} [label="branch ×{cnt}\\n[{len(tokens)}n]\\n{tok_str}",'
                   f' fillcolor="{col}", style="filled,rounded",'
                   f' fontcolor="white"];')
        out.append(f'  {anchor_from} -> {bid} [color="{col}", style=dashed];')
        out.append(f'  {bid} -> {anchor_to} [color="{col}", style=dashed];')
    out.append(_legend_dot())
    out.append('}')

    dot_path = out_base + ".dot"
    svg_path = out_base + ".svg"
    png_path = out_base + ".png"
    with open(dot_path, "w") as f:
        f.write("\n".join(out))
    import subprocess
    try:
        subprocess.check_call(["dot", "-Tsvg", dot_path, "-o", svg_path])
        subprocess.check_call(["dot", "-Tpng", dot_path, "-o", png_path])
        return svg_path, png_path
    except Exception as e:
        print(f"[skeleton] dot failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Figure 3: full conditional graph — branches expanded as a trie with
# probability labels on every edge
# ---------------------------------------------------------------------------

def render_full_conditional_graph(skeleton, per_inv, classes,
                                   total_invocations, out_base, title):
    """For each skeleton position p, build a trie of insertion blocks
    and render it between sk_p and sk_(p+1) with probability labels.
    Edges from skeleton to trie root: absolute frequency over total
    invocations. Edges within the trie: the count of invocations
    flowing through that edge (also absolute, so probabilities monotone
    decrease as you go deeper)."""

    # Aggregate insertions per skeleton position
    blocks_by_pos = defaultdict(Counter)  # pos → Counter[tuple(tokens)] → count
    direct_count_by_pos = Counter()       # invocations with NO insertion at p
    total_count = total_invocations

    for sig, inv_list in per_inv.items():
        for inv in inv_list:
            seq = inv["cpu_seq"]
            flags = align_to_skeleton(seq, skeleton)
            seen_positions = set()
            for skel_idx, tokens in insertions_between_skeleton(seq, flags):
                blocks_by_pos[skel_idx][tuple(tokens)] += 1
                seen_positions.add(skel_idx)
            # For positions where this invocation had NO insertion, count direct
            for p in range(-1, len(skeleton)):
                if p not in seen_positions:
                    direct_count_by_pos[p] += 1

    out = ["digraph full_cond {",
           '  rankdir=LR;',
           '  node [shape=box, style="filled,rounded", fontname="Helvetica",'
           ' fontsize=8, margin="0.04,0.02"];',
           '  edge [fontname="Helvetica", fontsize=7];',
           f'  label="{title}"; labelloc=t; labeljust=l; fontsize=10;',
           '  graph [nodesep=0.10, ranksep=0.40];',
           '']

    # Skeleton (full, not truncated, since user wants the entirety)
    for i, tok in enumerate(skeleton):
        out.append(f'  sk{i} [label="{i}\\n{render_label(tok)}", fillcolor="#BBDEFB"];')

    # START / END pseudo-nodes
    out.append('  sk_start [label="START\\n(pseudo, before any\\nskeleton match)", '
               'fillcolor="#E1F5FE", style=filled, shape=note];')
    out.append('  sk_end [label="END\\n(pseudo, after last\\nskeleton match)", '
               'fillcolor="#E1F5FE", style=filled, shape=note];')
    if skeleton:
        out.append('  sk_start -> sk0 [color="#1976D2", penwidth=2];')
        out.append(f'  sk{len(skeleton)-1} -> sk_end [color="#1976D2", penwidth=2];')

    # Skeleton order edges
    for i in range(len(skeleton) - 1):
        # Direct count = invocations that go sk_i -> sk_(i+1) without insertion
        direct = direct_count_by_pos[i]
        pct = 100.0 * direct / total_count if total_count else 0
        out.append(
            f'  sk{i} -> sk{i+1} [color="#1976D2", penwidth=2, '
            f'label="{direct}/{total_count} ({pct:.0f}%)", fontcolor="#1976D2"];')

    # Per-position trie
    node_counter = [0]
    def new_id():
        nid = f"t{node_counter[0]}"
        node_counter[0] += 1
        return nid

    def render_trie_node_children(node, dot_id, anchor_to):
        # Internal edges use CONDITIONAL probability — "given you've
        # reached `node`, what's the chance of taking this transition?".
        # Edges OUT of `node` partition node.count into:
        #   ends_here + sum(child.count for child in children)
        if node.ends_here > 0:
            cond = 100.0 * node.ends_here / node.count if node.count else 0
            out.append(
                f'  {dot_id} -> {anchor_to} [label="end ×{node.ends_here} '
                f'({cond:.0f}%)", style=dashed, color="#666", fontcolor="#666"];')
        for tok, child in node.children.items():
            cid = new_id()
            out.append(f'  {cid} [label="{render_label(tok)}", fillcolor="#FFE0B2"];')
            cond = 100.0 * child.count / node.count if node.count else 0
            out.append(f'  {dot_id} -> {cid} [label="×{child.count} ({cond:.0f}%)"];')
            render_trie_node_children(child, cid, anchor_to)

    for skel_idx in sorted(blocks_by_pos):
        block_list = list(blocks_by_pos[skel_idx].items())
        if not block_list:
            continue
        # blocks_by_pos[skel_idx] is Counter[tuple] -> count
        block_pairs = [(list(toks), cnt) for toks, cnt in block_list]
        trie = build_position_trie(block_pairs)
        if skel_idx == -1:
            anchor_from = "sk_start"
            anchor_to = "sk0" if skeleton else "sk_end"
        elif skel_idx == len(skeleton) - 1:
            anchor_from = f"sk{skel_idx}"
            anchor_to = "sk_end"
        else:
            anchor_from = f"sk{skel_idx}"
            anchor_to = f"sk{skel_idx + 1}"

        # First-level branches from anchor_from (skeleton node).
        # These edges report ABSOLUTE probability (% of all kept
        # invocations entering this branch). Internal edges within the
        # trie report CONDITIONAL probability (% of those who reached
        # the parent node).
        for tok, child in trie.children.items():
            cid = new_id()
            out.append(f'  {cid} [label="{render_label(tok)}", fillcolor="#FFE0B2"];')
            pct = 100.0 * child.count / total_count
            out.append(f'  {anchor_from} -> {cid} [label="×{child.count} (abs {pct:.0f}%)",'
                       f' style=dashed, color="#888"];')
            render_trie_node_children(child, cid, anchor_to)

    out.append(_legend_dot())
    out.append('}')

    dot_path = out_base + ".dot"
    svg_path = out_base + ".svg"
    png_path = out_base + ".png"
    with open(dot_path, "w") as f:
        f.write("\n".join(out))
    import subprocess
    try:
        subprocess.check_call(["dot", "-Tsvg", dot_path, "-o", svg_path])
        subprocess.check_call(["dot", "-Tpng", dot_path, "-o", png_path])
        return svg_path, png_path
    except Exception as e:
        print(f"[skeleton] dot failed: {e}")
        return None, None


def render_conditional_graph(skeleton, per_inv, classes, out_base, title):
    """Figure 1: skeleton backbone + top-15 branch summary boxes."""
    # Aggregate insertion blocks: key = (skeleton_idx, tuple(block_tokens))
    block_counts = Counter()
    block_class = defaultdict(Counter)
    for sig, inv_list in per_inv.items():
        cls = classes.get(sig, "variant")
        seq = inv_list[0]["cpu_seq"]
        flags = align_to_skeleton(seq, skeleton)
        for skel_idx, tokens in insertions_between_skeleton(seq, flags):
            key = (skel_idx, tuple(tokens))
            block_counts[key] += len(inv_list)
            block_class[key][cls] += len(inv_list)

    short = SHORTEN

    out = ["digraph skeleton {",
           '  rankdir=LR;',
           '  node [shape=box, style="filled,rounded", fontname="Helvetica",'
           ' fontsize=8, margin="0.05,0.02"];',
           '  edge [fontname="Helvetica", fontsize=7];',
           f'  label="{title}"; labelloc=t; labeljust=l; fontsize=10;',
           '  graph [nodesep=0.15, ranksep=0.25];',
           '',
           '  subgraph cluster_skel {',
           '    label="skeleton (common to all invocations)";',
           '    style="filled,rounded"; fillcolor="#F0F7FF";',
           '    fontname="Helvetica-Bold"; fontsize=9;',
           ]

    # Skeleton nodes — show first ~25 with index, then add ellipsis
    show_max = 25
    n_skel = len(skeleton)
    show_idxs = list(range(min(show_max, n_skel)))
    if n_skel > show_max:
        # Show first 12 and last 8 with ... in between
        show_idxs = list(range(12)) + list(range(n_skel - 8, n_skel))

    for idx in show_idxs:
        out.append(f'    sk{idx} [label="{idx}\\n{short(skeleton[idx])}",'
                   f' fillcolor="#BBDEFB"];')

    # skeleton order edges
    prev = None
    for idx in show_idxs:
        if prev is not None:
            if idx == prev + 1:
                out.append(f'    sk{prev} -> sk{idx} [color="#1976D2", penwidth=2];')
            else:
                out.append(f'    sk{prev} -> sk{idx} [color="#1976D2", '
                           f'penwidth=2, label="…(+{idx-prev-1})", style=dashed];')
        prev = idx
    out.append('  }')

    # Insertion blocks — show top branches by count
    # Group by skeleton_idx for layout clarity
    blocks_sorted = block_counts.most_common(15)
    for bi, ((skel_idx, tokens), cnt) in enumerate(blocks_sorted):
        # Pick predominant class colour for this block
        cls = block_class[(skel_idx, tokens)].most_common(1)[0][0]
        col = CLASS_COLORS.get(cls, "#999999")
        # If the insertion is long, show only first 4 and length
        if len(tokens) <= 4:
            tok_str = "\\n".join(short(t) for t in tokens)
        else:
            tok_str = "\\n".join(short(t) for t in tokens[:3])
            tok_str += f"\\n+{len(tokens)-3} more"
        # Find anchor: the skeleton node visible after skel_idx
        # If skel_idx == -1: anchor is sk(first visible)
        if skel_idx >= 0 and skel_idx in show_idxs:
            anchor_from = f"sk{skel_idx}"
        else:
            # before the first shown skeleton node
            anchor_from = f"sk{show_idxs[0]}"
        anchor_to_pos = skel_idx + 1
        if anchor_to_pos in show_idxs:
            anchor_to = f"sk{anchor_to_pos}"
        elif show_idxs:
            anchor_to = f"sk{show_idxs[-1]}"
        else:
            anchor_to = f"sk0"

        bid = f"br{bi}"
        out.append(f'  {bid} [label="branch ×{cnt}\\n[{len(tokens)}n]\\n{tok_str}",'
                   f' fillcolor="{col}", style="filled,rounded",'
                   f' fontcolor="white"];')
        out.append(f'  {anchor_from} -> {bid} [color="{col}", style=dashed];')
        out.append(f'  {bid} -> {anchor_to} [color="{col}", style=dashed];')

    out.append(_legend_dot())
    out.append('}')

    dot_path = out_base + ".dot"
    svg_path = out_base + ".svg"
    png_path = out_base + ".png"
    with open(dot_path, "w") as f:
        f.write("\n".join(out))
    import subprocess
    try:
        subprocess.check_call(["dot", "-Tsvg", dot_path, "-o", svg_path])
        subprocess.check_call(["dot", "-Tpng", dot_path, "-o", png_path])
        return svg_path, png_path
    except Exception as e:
        print(f"[skeleton] dot failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Some big-variant invocations produce deep tries (>1000 nodes for
    # anomalies in F#1684/F#1677). Python's default 1000-frame limit
    # blows the recursive trie renderer; raise it.
    sys.setrecursionlimit(50000)

    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("f_id", type=int)
    ap.add_argument("-o", "--outdir", default="dag_out")
    ap.add_argument("--target", choices=["cpu", "all"], default="cpu",
                    help="LCS over CPU-only sequence, or full PE sequence")
    ap.add_argument("--mode", choices=["subseq", "substr"], default="subseq",
                    help="single-figure mode: subseq or substr")
    ap.add_argument("--figure", choices=["1", "2", "3", "all"], default="all",
                    help="1 = skeleton (subseq) + collapsed branches; "
                         "2 = subseq skeleton with substring portion "
                         "highlighted; "
                         "3 = full conditional graph (skeleton + per-position "
                         "branch trie with probability labels). "
                         "'all' produces all three (single output base).")
    ap.add_argument("--sigma", type=float, default=2.0)
    ap.add_argument("--min-tokens", type=int, default=5,
                    help="Drop invocations with sequence shorter than this "
                         "before computing LCS. Trivial early-return paths "
                         "(1–2 token shapes) collapse the LCS to 1 and "
                         "should be analysed separately. Default 5.")
    ap.add_argument("--exclude-class", action="append", default=[],
                    choices=["dominant", "init", "shutdown", "anomaly", "variant"],
                    help="Drop invocations whose signature is in this class "
                         "(repeatable). Useful for skeletonising the "
                         "non-anomaly main body.")
    args = ap.parse_args()

    invs, v = _extract_invocations(args.session, args.f_id)
    if not invs:
        raise SystemExit("No invocations.")

    # Augment each invocation with its cpu_seq for re-use
    seqs = []
    for inv in invs:
        inv["cpu_seq"] = cpu_names(inv["dag"], mode=args.target)
        seqs.append(inv["cpu_seq"])

    # Class info from phase classifier
    by_sig = _per_sig_stats(invs)
    classes, _ = _classify(by_sig, len(invs), sigma=args.sigma)

    # per_inv built after keep_idx is computed, see below.
    per_inv = defaultdict(list)

    # Filter invocations before LCS
    excluded_classes = set(args.exclude_class)
    keep_idx = []
    dropped = []
    for i, inv in enumerate(invs):
        seq = inv["cpu_seq"]
        cls = classes.get(inv["signature"], "variant")
        reason = None
        if len(seq) < args.min_tokens:
            reason = f"short ({len(seq)} < {args.min_tokens})"
        elif cls in excluded_classes:
            reason = f"class={cls}"
        if reason:
            dropped.append((inv["signature"], cls, len(seq), reason))
        else:
            keep_idx.append(i)
    if dropped:
        print(f"[skel] dropped {len(dropped)} invocations:")
        seen = set()
        for sig, cls, n, reason in dropped:
            if sig in seen: continue
            seen.add(sig)
            n_inv = sum(1 for s2,_,_,_ in dropped if s2 == sig)
            print(f"        {sig}  cls={cls:<8} seq_len={n}  ×{n_inv}  ({reason})")

    if not keep_idx:
        raise SystemExit("All invocations filtered out — relax --min-tokens / --exclude-class.")

    kept_seqs = [seqs[i] for i in keep_idx]

    # Per-sig invocation lookup — only KEPT invocations, so figures 1/2/3
    # are consistent with the filtering done before LCS. Including the
    # dropped trivials would inflate "direct" counts past total (>100%).
    keep_set = set(keep_idx)
    for i, inv in enumerate(invs):
        if i in keep_set:
            per_inv[inv["signature"]].append(inv)

    # Compute both subseq + substring (figures 2/3 need both;
    # figure 1 needs only subseq)
    print(f"[skel] computing LCS subseq across {len(kept_seqs)} kept "
          f"invocations (target={args.target})…")
    skeleton_subseq = lcs_multi(kept_seqs)
    print(f"[skel] LCS subseq length = {len(skeleton_subseq)}")

    skeleton_substr = []
    if args.figure in ("2", "all"):
        print(f"[skel] computing LCS substring (contiguous) …")
        skeleton_substr = lcsubstr_multi(kept_seqs)
        print(f"[skel] LCS substring length = {len(skeleton_substr)}")

    # The "skeleton" used downstream for skel% reporting follows --mode
    # (default subseq, switch to substr only if user asked for it
    # explicitly via --mode and --figure 1)
    skeleton = (skeleton_subseq if args.mode == "subseq"
                else (skeleton_substr if skeleton_substr
                      else lcsubstr_multi(kept_seqs)))
    print(f"[skel] skeleton length = {len(skeleton)}")
    if not skeleton:
        print("[skel] empty skeleton — invocations share NO common token sequence.")
        return

    # Per-invocation alignment + insertions
    summary = []
    for inv in invs:
        seq = inv["cpu_seq"]
        flags = align_to_skeleton(seq, skeleton)
        n_skel = sum(flags)
        n_ins  = len(seq) - n_skel
        cls = classes.get(inv["signature"], "variant")
        summary.append({
            "sig": inv["signature"],
            "class": cls,
            "n_seq": len(seq),
            "n_skel": n_skel,
            "n_ins": n_ins,
            "skel_pct": n_skel / len(seq) * 100,
        })

    # Console + text summary
    summary.sort(key=lambda r: (-r["n_seq"], r["sig"]))
    out_lines = [
        f"F#{args.f_id} — {v.A_v.get('label','')[:80]}",
        f"invocations={len(invs)}  signatures={len(by_sig)}  "
        f"target={args.target}",
        f"skeleton length: {len(skeleton)} tokens",
        "",
        "skeleton sequence (truncated to first 60):",
        "  " + " ".join(skeleton[:60]),
        ("  …" if len(skeleton) > 60 else ""),
        "",
        f"{'sig':<14} {'class':<9} {'seq':>5} {'skel':>5} {'ins':>5} "
        f"{'skel%':>6}",
    ]
    seen_sigs = set()
    for r in summary:
        if r["sig"] in seen_sigs: continue
        seen_sigs.add(r["sig"])
        out_lines.append(
            f"{r['sig']:<14} {r['class']:<9} {r['n_seq']:>5} "
            f"{r['n_skel']:>5} {r['n_ins']:>5} {r['skel_pct']:>5.1f}%")

    txt_path = os.path.join(args.outdir, f"skeleton_F{args.f_id}.txt")
    os.makedirs(args.outdir, exist_ok=True)
    with open(txt_path, "w") as f:
        f.write("\n".join(out_lines))
    print()
    print("\n".join(out_lines))
    print(f"\n[skel] → {txt_path}")

    base_root = os.path.join(args.outdir,
                              f"skeleton_F{args.f_id}_{args.target}")
    n_kept = len(keep_idx)

    if args.figure in ("1", "all"):
        title1 = (f"F#{args.f_id} fig1: backbone (LCS subseq, "
                  f"target={args.target}) — "
                  f"{len(invs)} inv ({n_kept} kept), {len(by_sig)} sigs, "
                  f"|skel|={len(skeleton_subseq)}")
        base = base_root + ("_fig1" if args.figure == "all" else "")
        svg, png = render_conditional_graph(
            skeleton_subseq, per_inv, classes, base, title1)
        if svg: print(f"[skel] fig1 → {svg} / {png}")

    if args.figure in ("2", "all") and skeleton_subseq:
        title2 = (f"F#{args.f_id} fig2: backbone with substring overlay — "
                  f"|subseq|={len(skeleton_subseq)}, "
                  f"|substring|={len(skeleton_substr)}")
        base = base_root + "_fig2"
        svg, png = render_skeleton_with_substring(
            skeleton_subseq, skeleton_substr, per_inv, classes, base, title2)
        if svg: print(f"[skel] fig2 → {svg} / {png}")

    if args.figure in ("3", "all") and skeleton_subseq:
        title3 = (f"F#{args.f_id} fig3: full conditional graph — "
                  f"{n_kept} invocations after filter. "
                  f"Edge labels: 'abs N%' = N% of all invocations took this "
                  f"branch from skeleton; 'N%' (no prefix) = conditional, "
                  f"N% of those reaching the parent node take this transition.")
        base = base_root + "_fig3"
        svg, png = render_full_conditional_graph(
            skeleton_subseq, per_inv, classes, n_kept, base, title3)
        if svg: print(f"[skel] fig3 → {svg} / {png}")


if __name__ == "__main__":
    main()
