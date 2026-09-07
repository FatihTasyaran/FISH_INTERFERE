→ paper/tables_v5/lean3x3/aw_phase_warmup.tex
→ paper/tables_v5/lean3x3/aw_phase_execution.tex
baseline: baseline_1: warm-up 4.6 s, execution 25.3 s; baseline_2: warm-up 6.4 s, execution 23.5 s; baseline_3: warm-up 4.8 s, execution 25.1 s
lttng: lttng_1: warm-up 5.8 s, execution 24.1 s; lttng_2: warm-up 7.1 s, execution 22.8 s; lttng_3: warm-up 6.8 s, execution 23.1 s
nsys: nsys_1: warm-up 6.4 s, execution 23.4 s; nsys_2: warm-up 8.1 s, execution 21.8 s; nsys_3: warm-up 7.0 s, execution 22.9 s

### (1)→(2) warm-up — mean of all diag values in the phase, pooled over 3 runs [n]

| value (reporting node) | baseline | lttng Δ | lttng+nsys Δ |
|---|--:|--:|--:|
| crop_box_filter_self proc [ms] | 2.29 [447] | 3.36 (+47%) [546] | 3.46 (+51%) [589] |
| crop_box_filter_self pipeline [ms] | 136.89 [447] | 139.89 (+2%) [546] | 139.84 (+2%) [589] |
| distortion_corrector proc [ms] | 4.50 [436] | 6.35 (+41%) [524] | 6.28 (+40%) [568] |
| distortion_corrector pipeline [ms] | 142.23 [436] | 145.85 (+3%) [524] | 145.19 (+2%) [568] |
| ring_outlier_filter proc [ms] | 2.50 [414] | 3.52 (+41%) [513] | 3.80 (+52%) [549] |
| ring_outlier_filter pipeline [ms] | 143.72 [414] | 147.56 (+3%) [513] | 147.37 (+3%) [549] |
| concatenate_data proc [ms] | 24.81 [145] | 34.95 (+41%) [181] | 31.76 (+28%) [199] |
| concatenate_data pipeline [ms] | 230.61 [145] | 254.97 (+11%) [181] | 249.62 (+8%) [199] |
| ndt sensor delay [ms] | 209.69 [75] | 237.68 (+13%) [55] | 226.61 (+8%) [73] |
| ndt iterations [#] | 1.19 [43] | 1.55 (+30%) [33] | 1.23 (+4%) [43] |
| ekf pose delay [ms] | 187.21 [211] | 198.53 (+6%) [200] | 186.05 (-1%) [247] |
| occupancy_grid proc [ms] | 3.69 [40] | 6.02 (+63%) [38] | 4.91 (+33%) [37] |
| centerpoint proc [ms] (GPU; >0 = inference ran) | 23.69 [5] | 29.03 (+23%) [9] | 24.98 (+5%) [11] |
| centerpoint consecutive delay [ms] | 0.00 [36] | 0.00 (→0.00) [44] | 0.00 (→0.00) [48] |
| map_based_prediction proc [ms] | 0.64 [132] | 0.76 (+19%) [161] | 0.75 (+17%) [179] |
| tracker detection delay [ms] | 233.49 [25] | 313.86 (+34%) [23] | 296.21 (+27%) [29] |
| monitor: Total Latency [ms] | 250.61 [36] | 250.69 (+0%) [70] | 254.95 (+2%) [76] |
| monitor: multi_object_tracker [ms] | — | — | — |
| monitor: map_based_prediction [ms] | 0.42 [31] | 0.70 (+67%) [68] | 0.95 (+128%) [73] |
| tsm objects rate [Hz] | 9.03 [30] | 9.11 (+1%) [65] | 8.96 (-1%) [26] |
| tsm obstacle pc rate [Hz] | 13.40 [3] | 9.07 (-32%) [3] | 8.79 (-34%) [2] |

### (2)→(3) execution — mean of all diag values in the phase, pooled over 3 runs [n]

| value (reporting node) | baseline | lttng Δ | lttng+nsys Δ |
|---|--:|--:|--:|
| crop_box_filter_self proc [ms] | 1.79 [2020] | 2.26 (+26%) [1772] | 2.32 (+30%) [1723] |
| crop_box_filter_self pipeline [ms] | 137.67 [2020] | 138.58 (+1%) [1772] | 138.23 (+0%) [1723] |
| distortion_corrector proc [ms] | 4.01 [1982] | 4.92 (+23%) [1738] | 5.02 (+25%) [1672] |
| distortion_corrector pipeline [ms] | 141.28 [1982] | 142.30 (+1%) [1738] | 142.59 (+1%) [1672] |
| ring_outlier_filter proc [ms] | 2.15 [1906] | 3.13 (+46%) [1676] | 3.20 (+49%) [1637] |
| ring_outlier_filter pipeline [ms] | 142.27 [1906] | 144.39 (+1%) [1676] | 144.42 (+2%) [1637] |
| concatenate_data proc [ms] | 13.78 [675] | 16.98 (+23%) [605] | 18.09 (+31%) [583] |
| concatenate_data pipeline [ms] | 201.03 [675] | 210.91 (+5%) [605] | 212.08 (+5%) [583] |
| ndt sensor delay [ms] | 192.75 [638] | 200.26 (+4%) [546] | 199.13 (+3%) [519] |
| ndt iterations [#] | 2.82 [630] | 2.94 (+5%) [523] | 2.98 (+6%) [508] |
| ekf pose delay [ms] | 218.85 [3232] | 211.69 (-3%) [2934] | 216.20 (-1%) [2879] |
| occupancy_grid proc [ms] | 3.81 [609] | 3.46 (-9%) [571] | 3.48 (-9%) [547] |
| centerpoint proc [ms] (GPU; >0 = inference ran) | 24.13 [226] | 25.14 (+4%) [233] | 25.97 (+8%) [233] |
| centerpoint consecutive delay [ms] | 0.00 [226] | 0.00 (→0.00) [233] | 0.00 (→0.00) [233] |
| map_based_prediction proc [ms] | 1.81 [686] | 2.26 (+25%) [602] | 2.48 (+37%) [600] |
| tracker detection delay [ms] | 250.29 [773] | 281.69 (+13%) [686] | 283.25 (+13%) [704] |
| monitor: Total Latency [ms] | 407.84 [133] | 442.81 (+9%) [163] | 440.27 (+8%) [169] |
| monitor: multi_object_tracker [ms] | 145.68 [133] | 189.40 (+30%) [163] | 181.12 (+24%) [169] |
| monitor: map_based_prediction [ms] | 1.89 [133] | 2.20 (+16%) [163] | 2.41 (+27%) [169] |
| tsm objects rate [Hz] | 9.89 [84] | 9.97 (+1%) [97] | 9.88 (-0%) [87] |
| tsm obstacle pc rate [Hz] | 9.11 [111] | 8.77 (-4%) [58] | 8.94 (-2%) [48] |
