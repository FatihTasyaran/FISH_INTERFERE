# syntax=docker/dockerfile:1.6
# =============================================================================
# fish-r2b-tensorrt-base — fish-r2b-base + NVIDIA TensorRT pre-installed
# =============================================================================
# Every DNN-based Isaac ROS benchmark depends on TensorRT (libnvinfer +
# libnvinfer-plugin + libnvonnxparsers + headers). Without it pre-baked,
# each per-benchmark image downloads ~5 GB of debs at build time
# (libnvinfer10 ~1.8 GB + libnvinfer-dev ~3.1 GB), ~10 min wallclock each.
# Multiplied across the ~14 DNN benchmarks in the sweep that's ~2+ hours
# of duplicated downloads + a hard hit on agent context budgets (every
# progress line eats tokens while the agent waits).
#
# This image bakes the TensorRT dev stack on top of fish-r2b-base once.
# Per-benchmark Dockerfiles for DNN benchmarks pick this as their FROM
# (selected by build_benchmark_image.sh based on benchmark category).
#
# Build:
#   docker build -t fish-r2b-tensorrt-base:latest -f docker/fish-r2b-tensorrt-base.Dockerfile .
# =============================================================================

FROM fish-r2b-base:latest

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libnvinfer-dev \
        libnvinfer-plugin-dev \
        libnvonnxparsers-dev && \
    rm -rf /var/lib/apt/lists/* && \
    test -f /usr/include/x86_64-linux-gnu/NvInferVersion.h
