→ paper/tables_v5/lean/aw_phase_warmup.tex
→ paper/tables_v5/lean/aw_phase_execution.tex
baseline: baseline_1: warm-up 4.8 s, execution 25.1 s
lttng: lttng_1: warm-up 6.8 s, execution 23.1 s
nsys: nsys_1: warm-up 6.5 s, execution 23.4 s

### (1)→(2) warm-up — mean of all diag values in the phase, pooled over 3 runs [n]

| value (reporting node) | baseline | lttng Δ | lttng+nsys Δ |
|---|--:|--:|--:|
| crop_box_filter_self proc [ms] | 1.94 [135] | 3.37 (+74%) [186] | 3.13 (+61%) [183] |
| crop_box_filter_self pipeline [ms] | 136.62 [135] | 139.88 (+2%) [186] | 139.72 (+2%) [183] |
| distortion_corrector proc [ms] | 4.65 [129] | 6.34 (+37%) [175] | 5.96 (+28%) [181] |
| distortion_corrector pipeline [ms] | 142.03 [129] | 146.96 (+3%) [175] | 145.24 (+2%) [181] |
| ring_outlier_filter proc [ms] | 2.70 [128] | 3.57 (+32%) [170] | 3.60 (+33%) [177] |
| ring_outlier_filter pipeline [ms] | 143.46 [128] | 148.90 (+4%) [170] | 147.53 (+3%) [177] |
| concatenate_data proc [ms] | 25.36 [42] | 31.08 (+23%) [61] | 45.66 (+80%) [61] |
| concatenate_data pipeline [ms] | 227.50 [42] | 241.82 (+6%) [61] | 272.51 (+20%) [61] |
| ndt sensor delay [ms] | 215.87 [22] | 227.30 (+5%) [21] | 255.69 (+18%) [20] |
| ndt iterations [#] | 1.30 [10] | 1.33 (+3%) [12] | 1.50 (+15%) [6] |
| ekf pose delay [ms] | 172.34 [51] | 165.07 (-4%) [82] | 71.33 (-59%) [53] |
| occupancy_grid proc [ms] | 3.57 [9] | 4.48 (+25%) [13] | 5.10 (+43%) [11] |
| centerpoint proc [ms] (GPU; >0 = inference ran) | — | — | — |
| centerpoint consecutive delay [ms] | 0.00 [9] | 0.00 (→0.00) [13] | 0.00 (→0.00) [17] |
| map_based_prediction proc [ms] | 0.75 [40] | 0.95 (+26%) [53] | 0.77 (+2%) [47] |
| tracker detection delay [ms] | 231.76 [3] | 358.72 (+55%) [8] | 336.24 (+45%) [4] |
| monitor: Total Latency [ms] | 257.54 [12] | 247.81 (-4%) [25] | 257.79 (+0%) [21] |
| monitor: multi_object_tracker [ms] | — | — | — |
| monitor: map_based_prediction [ms] | 0.17 [10] | 1.02 (+497%) [25] | 0.34 (+96%) [21] |
| tsm objects rate [Hz] | 8.94 [6] | 8.94 (+0%) [7] | 8.99 (+1%) [32] |
| tsm obstacle pc rate [Hz] | — | — | — |

### (2)→(3) execution — mean of all diag values in the phase, pooled over 3 runs [n]

| value (reporting node) | baseline | lttng Δ | lttng+nsys Δ |
|---|--:|--:|--:|
| crop_box_filter_self proc [ms] | 1.76 [686] | 2.38 (+35%) [581] | 2.33 (+32%) [593] |
| crop_box_filter_self pipeline [ms] | 137.63 [686] | 138.02 (+0%) [581] | 138.25 (+0%) [593] |
| distortion_corrector proc [ms] | 4.00 [667] | 4.95 (+24%) [584] | 4.86 (+21%) [593] |
| distortion_corrector pipeline [ms] | 141.26 [667] | 142.18 (+1%) [584] | 142.53 (+1%) [593] |
| ring_outlier_filter proc [ms] | 2.16 [651] | 3.19 (+48%) [572] | 2.85 (+32%) [570] |
| ring_outlier_filter pipeline [ms] | 142.50 [651] | 144.76 (+2%) [572] | 144.41 (+1%) [570] |
| concatenate_data proc [ms] | 12.09 [227] | 16.94 (+40%) [208] | 14.63 (+21%) [207] |
| concatenate_data pipeline [ms] | 199.19 [227] | 213.41 (+7%) [208] | 208.05 (+4%) [207] |
| ndt sensor delay [ms] | 191.55 [219] | 198.38 (+4%) [179] | 196.04 (+2%) [183] |
| ndt iterations [#] | 2.77 [219] | 2.94 (+6%) [176] | 2.98 (+8%) [182] |
| ekf pose delay [ms] | 224.80 [1100] | 214.52 (-5%) [986] | 218.64 (-3%) [968] |
| occupancy_grid proc [ms] | 3.78 [205] | 3.38 (-11%) [184] | 3.18 (-16%) [189] |
| centerpoint proc [ms] (GPU; >0 = inference ran) | 24.25 [69] | 26.05 (+7%) [63] | 25.88 (+7%) [68] |
| centerpoint consecutive delay [ms] | 0.00 [69] | 0.00 (→0.00) [63] | 0.00 (→0.00) [68] |
| map_based_prediction proc [ms] | 1.92 [226] | 2.57 (+34%) [203] | 2.37 (+24%) [209] |
| tracker detection delay [ms] | 250.86 [270] | 284.62 (+13%) [264] | 279.64 (+11%) [231] |
| monitor: Total Latency [ms] | 403.39 [54] | 443.74 (+10%) [43] | 450.94 (+12%) [45] |
| monitor: multi_object_tracker [ms] | 146.45 [54] | 194.62 (+33%) [43] | 206.59 (+41%) [45] |
| monitor: map_based_prediction [ms] | 1.82 [54] | 2.70 (+48%) [43] | 2.78 (+53%) [45] |
| tsm objects rate [Hz] | 9.88 [30] | 9.74 (-1%) [15] | 9.88 (+0%) [48] |
| tsm obstacle pc rate [Hz] | 9.36 [40] | 8.74 (-7%) [23] | 8.88 (-5%) [56] |
