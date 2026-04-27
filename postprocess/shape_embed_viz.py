#!/usr/bin/env python3
"""shape_embed_viz — embed all invocations of a callback in 2D.

For one F vertex (callback), re-extract every invocation's Typed DAG,
featurize it as a fixed-length vector, reduce to 2D (UMAP if available,
else PCA), and scatter-plot. Class colors come from
shape_phase_classifier so the embedding figure shares vocabulary with
the phase-class table.

Usage:
  python3 shape_embed_viz.py <session> <F#> [-o <outdir>]
                             [--method umap|pca]
                             [--time-trajectory]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

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
CLASS_ORDER = ["dominant", "init", "shutdown", "anomaly", "variant"]


def _api_vocab(invocations, top_k=20):
    """Build a bag-of-API vocabulary across all invocations: top_k most
    frequent CPU node names."""
    cnt = Counter()
    for inv in invocations:
        for n in inv["dag"]["nodes"]:
            if n["pe"] == "CPU":
                cnt[n["name"]] += 1
    return [name for name, _ in cnt.most_common(top_k)]


def _featurize(invocations, vocab):
    """Per-invocation feature vector. Layout:
        [n_cpu, n_sm, n_ce, total,
         e_launch, e_stream_fifo, e_cpu_order,
         h2d, d2h, other_mc,
         n_streams, depth_proxy, branch_proxy,
         api_count_for_each_vocab_entry...]
    """
    feats = []
    for inv in invocations:
        d = inv["dag"]
        n_cpu = sum(1 for n in d["nodes"] if n["pe"] == "CPU")
        n_sm  = sum(1 for n in d["nodes"] if n["pe"] == "SM")
        n_ce  = sum(1 for n in d["nodes"] if n["pe"] == "CE")
        total = len(d["nodes"])

        e_launch = e_fifo = e_order = 0
        for e in d["edges"]:
            kind = e[2] if len(e) > 2 else ""
            if kind == "launch":         e_launch += 1
            elif kind == "stream_fifo":  e_fifo   += 1
            elif kind == "cpu_order":    e_order  += 1

        h2d = d2h = other_mc = 0
        for n in d["nodes"]:
            if n["pe"] != "CE": continue
            nm = n.get("name", "")
            if nm == "memcpy_H2D":          h2d += 1
            elif nm == "memcpy_D2H":        d2h += 1
            elif nm.startswith("memcpy_"): other_mc += 1

        n_streams = len(set(n.get("stream") for n in d["nodes"]
                             if n.get("stream") is not None))

        # Depth proxy: longest CPU chain (cpu_order edges connect them in seq)
        # — approximated by count of cpu_order edges (each = one step deeper)
        depth_proxy = e_order
        # Branch proxy: max out-degree in the launch-edge subgraph
        out_deg = Counter()
        for e in d["edges"]:
            if len(e) > 2 and e[2] == "launch":
                out_deg[e[0]] += 1
        branch_proxy = max(out_deg.values()) if out_deg else 0

        # API bag (only CPU nodes)
        cpu_names = Counter(n["name"] for n in d["nodes"] if n["pe"] == "CPU")
        api_bag = [cpu_names.get(v, 0) for v in vocab]

        vec = [n_cpu, n_sm, n_ce, total,
               e_launch, e_fifo, e_order,
               h2d, d2h, other_mc,
               n_streams, depth_proxy, branch_proxy] + api_bag
        feats.append(vec)
    return np.array(feats, dtype=float)


def _project_pca(X, k=2):
    Xc = X - X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    Xc = Xc / sd
    U, S, Vt = np.linalg.svd(Xc, full_matrices=False)
    return Xc @ Vt.T[:, :k]


def _project_umap(X, k=2):
    try:
        import umap
        # Standardize first — same prep as PCA
        sd = X.std(axis=0)
        sd[sd == 0] = 1.0
        Xn = (X - X.mean(axis=0)) / sd
        n = len(Xn)
        n_neighbors = max(2, min(15, n - 1))
        reducer = umap.UMAP(
            n_components=k,
            n_neighbors=n_neighbors,
            min_dist=0.1,
            random_state=42,
        )
        return reducer.fit_transform(Xn)
    except Exception as e:
        print(f"[viz] UMAP failed ({e}), falling back to PCA")
        return _project_pca(X, k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("f_id", type=int)
    ap.add_argument("-o", "--outdir", default="dag_out")
    ap.add_argument("--method", choices=["pca", "umap"], default="umap")
    ap.add_argument("--time-trajectory", action="store_true",
                    help="Connect invocations in time order with arrows")
    ap.add_argument("--sigma", type=float, default=2.0)
    ap.add_argument("--shutdown-tail", type=float, default=0.05)
    ap.add_argument("--top-k", type=int, default=20,
                    help="API vocab size (top-K most frequent CPU names)")
    args = ap.parse_args()

    invocations, v = _extract_invocations(args.session, args.f_id)
    if not invocations:
        raise SystemExit("No invocations recovered.")

    # Classes (per signature)
    by_sig = _per_sig_stats(invocations)
    classes, _ = _classify(by_sig, len(invocations),
                           sigma=args.sigma,
                           shutdown_tail=args.shutdown_tail)

    # Build features and project
    vocab = _api_vocab(invocations, top_k=args.top_k)
    X = _featurize(invocations, vocab)
    print(f"[viz] features: {X.shape}")
    if args.method == "umap":
        Y = _project_umap(X)
    else:
        Y = _project_pca(X)

    # Plot
    fig, ax = plt.subplots(figsize=(11, 8))
    fig.suptitle(
        f"F#{args.f_id} — invocation embedding "
        f"({args.method.upper()})\n"
        f"{len(invocations)} invocations · {len(by_sig)} distinct shapes",
        fontsize=11)

    # Time-trajectory arrows (drawn first, behind points)
    if args.time_trajectory:
        order = sorted(range(len(invocations)),
                       key=lambda i: invocations[i]["start_ns"])
        for a, b in zip(order[:-1], order[1:]):
            ax.annotate("", xy=Y[b], xytext=Y[a],
                        arrowprops=dict(arrowstyle="->", color="#CCCCCC",
                                        lw=0.6, alpha=0.5))

    # Points: color = class, size = invocation_count for that sig (so dups
    # at same point show their multiplicity)
    sig_to_xy = {}
    for i, inv in enumerate(invocations):
        sig_to_xy.setdefault(inv["signature"], []).append(Y[i])

    drawn_classes = set()
    for sig, xys in sig_to_xy.items():
        cls = classes[sig]
        col = CLASS_COLORS.get(cls, "#999999")
        # All invocations of this sig collapse to identical features,
        # so they sit at the same (x,y). Size = sqrt(count).
        n = len(xys)
        x, y = xys[0]
        size = 80 + 30 * (n - 1)
        label = cls if cls not in drawn_classes else None
        ax.scatter([x], [y], c=col, s=size, alpha=0.7,
                   edgecolors="white", linewidths=1, label=label, zorder=3)
        drawn_classes.add(cls)
        # sig short label (last 6 chars) + count
        ax.annotate(f"{sig[:6]} ({n})",
                    xy=(x, y), xytext=(5, 5),
                    textcoords="offset points",
                    fontsize=7, color="#333333", zorder=4)

    # Legend
    handles = []
    for c in CLASS_ORDER:
        if c in drawn_classes:
            handles.append(plt.Line2D([0], [0], marker="o", linestyle="",
                                       color=CLASS_COLORS[c], label=c,
                                       markersize=10))
    ax.legend(handles=handles, loc="upper right", title="phase class",
              framealpha=0.9)

    ax.set_xlabel(f"{args.method.upper()} dim 1")
    ax.set_ylabel(f"{args.method.upper()} dim 2")
    ax.grid(True, alpha=0.3)

    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.join(args.outdir,
                        f"embed_F{args.f_id}_{args.method}")
    plt.tight_layout()
    plt.savefig(base + ".svg", bbox_inches="tight")
    plt.savefig(base + ".png", dpi=140, bbox_inches="tight")
    print(f"[viz] → {base}.svg / .png")


if __name__ == "__main__":
    main()
