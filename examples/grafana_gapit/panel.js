// =============================================================================
// FISH Graph Explorer — gapit-htmlgraphics-panel JS
// =============================================================================
// Port of postprocess/fish_viz.html. Reads data from Grafana's query result
// (`data.series[]`), builds the same nodes/edges shape that fish_viz.html
// expected, then runs the same D3 force/tree/stair render logic.
//
// gapit-htmlgraphics-panel injects: data, htmlNode, options, theme.
// =============================================================================

(function () {
  if (typeof d3 === 'undefined') {
    const s = document.createElement('script');
    s.src = 'https://d3js.org/d3.v7.min.js';
    s.onload = function () { renderPanel(); };
    document.head.appendChild(s);
  } else {
    renderPanel();
  }

  function buildRaw(series) {
    const findFrame = (id) =>
      series.find(s => s.refId === id) ||
      series.find(s => (s.name || '').toLowerCase() === id.toLowerCase());
    const nf = findFrame('A') || series[0];
    const ef = findFrame('B') || series[1];

    function frameToRows(frame) {
      if (!frame) return [];
      // Grafana DataFrame: fields = [{name, values: [...]}, ...]; length = row count
      const cols = {};
      (frame.fields || []).forEach(f => { cols[f.name] = f.values; });
      const n = frame.length || (Object.values(cols)[0] ? Object.values(cols)[0].length : 0);
      const rows = [];
      for (let i = 0; i < n; i++) {
        const row = {};
        for (const k of Object.keys(cols)) {
          const v = cols[k];
          row[k] = (Array.isArray(v) || (v && typeof v[i] !== 'undefined')) ? v[i] : null;
        }
        rows.push(row);
      }
      return rows;
    }

    function parseMaybeJSON(v) {
      if (v === null || v === undefined) return null;
      if (typeof v === 'object') return v;
      if (typeof v === 'string') {
        const t = v.trim();
        if (!t) return null;
        try { return JSON.parse(t); } catch (e) { return v; }
      }
      return v;
    }

    const nodes = frameToRows(nf).map(r => {
      const attrs = parseMaybeJSON(r.attrs) || {};
      const children = parseMaybeJSON(r.children) || [];
      const out = {
        id: r.id,
        type: r.type,
        label: r.label || '',
        level: typeof r.level === 'number' ? r.level : Number(r.level) || 0,
        children: Array.isArray(children) ? children : [],
        external: !!r.external,
      };
      if (r.pid != null) out.pid = r.pid;
      if (r.full_name) out.full_name = r.full_name;
      if (r.etype) out.etype = r.etype;
      if (r.ptype) out.ptype = r.ptype;
      if (r.cb_addr) out.cb_addr = r.cb_addr;
      // attrs may carry aspects, executor_type, num_threads, callback_group, ...
      if (attrs && typeof attrs === 'object') Object.assign(out, attrs);
      return out;
    });

    const edges = frameToRows(ef).map(r => {
      const attrs = parseMaybeJSON(r.attrs) || {};
      const out = {
        source: r.source,
        target: r.target,
        rel: r.rel,
        level: r.level,
      };
      if (attrs && typeof attrs === 'object') Object.assign(out, attrs);
      return out;
    });

    return { nodes, edges };
  }

  function renderPanel() {
    const root = htmlNode.querySelector('.fish-viz-root');
    if (!root) return;
    // Clear any previous render
    const svgEl = root.querySelector('svg.fv-svg');
    while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);

    const raw = buildRaw(data.series || []);
    if (!raw.nodes.length) {
      root.querySelector('.fv-stats').textContent = 'No graph data (check session_id / scope).';
      return;
    }

    const R = {CN:24, EX:20, N:14, E:10, F:7};
    const LAYER_GAP = 180;
    const STAIR_STEP = 16;
    const NODE_SPACING = 90;
    const W = root.clientWidth || 1200;
    const H = root.clientHeight || 700;

    let mode = 'expand';
    let curLayout = 'stair';
    let layerDepth = 0;
    let stairMode = '3';
    let showExt = false;
    let showLbl = true;
    let showContain = true;
    let simulation = null;
    const exp = new Set();

    const byId = {};
    raw.nodes.forEach(n => { byId[n.id] = n; });
    const parentOf = {};
    const childrenOf = {};
    raw.nodes.forEach(n => {
      (n.children || []).forEach(c => {
        parentOf[c] = n.id;
        (childrenOf[n.id] = childrenOf[n.id] || []).push(c);
      });
    });

    const commEdges = raw.edges
      .filter(e => e.rel === 'comm')
      .map(e => ({
        s: e.source, d: e.target,
        topic: e.topic || e.service || '',
        nature: e.nature || 'msg',
        ext: !!e.external,
        level: e.level || ''
      }));

    function isVis(id) {
      const n = byId[id]; if (!n) return false;
      if (n.external) return showExt;
      if (mode === 'layer') {
        const eff = layerDepth >= 99 ? Infinity : layerDepth;
        return n.level <= eff;
      }
      const p = parentOf[id];
      return p === undefined || exp.has(p);
    }
    function visAnc(id) {
      if (isVis(id)) return id;
      const p = parentOf[id];
      return p !== undefined ? visAnc(p) : null;
    }
    const getVisNodes = () => raw.nodes.filter(n => isVis(n.id));

    function getVisCommEdges() {
      const m = {};
      commEdges.forEach(e => {
        if (e.ext && !showExt) return;
        const s = visAnc(e.s), d = visAnc(e.d);
        if (!s || !d || s === d) return;
        const dk = s + '->' + d;
        if (!m[dk]) m[dk] = {source: s, target: d, topics: new Set(), cnt: 0, nature: e.nature, ext: e.ext};
        m[dk].cnt++;
        if (e.topic) m[dk].topics.add(e.topic);
      });
      return Object.values(m).map(e => ({...e, topics: [...e.topics]}));
    }
    function getVisContainEdges() {
      if (!showContain) return [];
      const out = [];
      raw.nodes.forEach(n => {
        if (!isVis(n.id)) return;
        (childrenOf[n.id] || []).forEach(cid => {
          if (isVis(cid)) {
            out.push({source: n.id, target: cid, nature: 'contains', cnt: 1, topics: [], ext: false});
          }
        });
      });
      return out;
    }
    function collapse(id) {
      exp.delete(id);
      (childrenOf[id] || []).forEach(c => { if (exp.has(c)) collapse(c); });
    }
    function labelText(d) {
      if (!showLbl) return '';
      let l = d.label || '';
      if (l.startsWith('ext:')) l = l.slice(4);
      const p = l.split('/'); l = p[p.length - 1] || l;
      return l.length > 22 ? l.slice(0, 20) + '..' : l;
    }

    const svg = d3.select(svgEl);
    const g = svg.append('g');
    svg.call(d3.zoom().scaleExtent([.03, 12]).on('zoom', e => g.attr('transform', e.transform)));

    const defs = svg.append('defs');
    defs.append('marker').attr('id', 'fv-arr').attr('viewBox', '0 -5 10 10')
      .attr('refX', 22).attr('refY', 0).attr('markerWidth', 5).attr('markerHeight', 5)
      .attr('orient', 'auto').append('path').attr('d', 'M0,-4L10,0L0,4').attr('fill', '#4A90D9');
    defs.append('marker').attr('id', 'fv-arr-f').attr('viewBox', '0 -5 10 10')
      .attr('refX', 10).attr('refY', 0).attr('markerWidth', 5).attr('markerHeight', 5)
      .attr('orient', 'auto').append('path').attr('d', 'M0,-4L10,0L0,4').attr('fill', '#4A90D9');
    defs.append('marker').attr('id', 'fv-arr-c').attr('viewBox', '0 -4 8 8')
      .attr('refX', 18).attr('refY', 0).attr('markerWidth', 4).attr('markerHeight', 4)
      .attr('orient', 'auto').append('path').attr('d', 'M0,-3L8,0L0,3').attr('fill', '#999');

    function computeTreePositions(vn, useStair) {
      const layers = {CN:[], EX:[], N:[], E:[], F:[]};
      const extNodes = [];
      vn.forEach(n => {
        if (n.external) extNodes.push(n);
        else if (layers[n.type]) layers[n.type].push(n);
      });
      layers.CN.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
      layers.EX.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
      for (const t of ['N', 'E', 'F']) {
        layers[t].sort((a, b) => {
          const pa = parentOf[a.id] || 0, pb = parentOf[b.id] || 0;
          return pa !== pb ? pa - pb : (a.label || '').localeCompare(b.label || '');
        });
      }
      const margin = 60;
      let currentY = 80;
      for (const type of ['CN', 'EX', 'N', 'E', 'F']) {
        const ns = layers[type];
        if (!ns.length) continue;
        const spacing = Math.max(NODE_SPACING, Math.min(200, (W - 2 * margin) / ns.length));
        const startX = Math.max(margin, (W - ns.length * spacing) / 2);
        ns.forEach((n, i) => {
          n.x = startX + i * spacing;
          if (useStair) {
            n.y = stairMode === 'inf' ? currentY + i * STAIR_STEP
                : stairMode === '3'   ? currentY + (i % 3) * STAIR_STEP
                : currentY;
          } else { n.y = currentY; }
        });
        currentY = ns.reduce((m, n) => Math.max(m, n.y), currentY) + LAYER_GAP;
      }
      if (extNodes.length) {
        extNodes.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
        const spacing = Math.max(60, Math.min(150, (W - 2 * margin) / extNodes.length));
        const startX = Math.max(margin, (W - extNodes.length * spacing) / 2);
        extNodes.forEach((n, i) => {
          n.x = startX + i * spacing;
          n.y = currentY + (useStair && stairMode === '3' ? (i % 3) * STAIR_STEP
              : useStair && stairMode === 'inf' ? i * 8 : 0);
        });
      }
    }

    function orthoPath(sx, sy, tx, ty) {
      if (Math.abs(sy - ty) < 5) {
        const midX = (sx + tx) / 2, rise = Math.min(-40, -Math.abs(tx - sx) * 0.15);
        return `M${sx},${sy} Q${midX},${sy + rise} ${tx},${ty}`;
      }
      const midY = (sy + ty) / 2;
      return `M${sx},${sy} L${sx},${midY} L${tx},${midY} L${tx},${ty}`;
    }
    const straightPath = (sx, sy, tx, ty) => `M${sx},${sy}L${tx},${ty}`;
    function forceLinkPath(d) {
      const s = d.source, t = d.target;
      if (!s || !t || s.x == null || t.x == null) return '';
      const dx = t.x - s.x, dy = t.y - s.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const tr = (R[t.type] || 8) + 8;
      return `M${s.x},${s.y}L${t.x - dx / dist * tr},${t.y - dy / dist * tr}`;
    }
    function nodeClass(d) {
      let c = 'node ' + d.type;
      if (d.external) c += ' ext';
      if (mode === 'expand' && childrenOf[d.id] && !d.external) {
        c += exp.has(d.id) ? ' expanded' : ' expandable';
      }
      return c;
    }
    function dedupeEdges(commE, containE) {
      const containKeys = new Set(containE.map(e => e.source + '->' + e.target));
      const filteredComm = commE.filter(e => !containKeys.has(e.source + '->' + e.target) &&
                                             !containKeys.has(e.target + '->' + e.source));
      return {comm: filteredComm, contain: containE};
    }

    function rebuild() {
      if (simulation) { simulation.stop(); simulation = null; }
      const vn = getVisNodes();
      const rawComm = getVisCommEdges();
      const rawContain = getVisContainEdges();
      const {comm: commE, contain: containE} = dedupeEdges(rawComm, rawContain);
      g.selectAll('*').remove();
      const vnMap = {};
      vn.forEach(n => { vnMap[n.id] = n; });
      if (curLayout === 'force') renderForce(vn, commE, containE, vnMap);
      else { computeTreePositions(vn, curLayout === 'stair'); renderTree(vn, commE, containE, vnMap); }
      const tc = {};
      vn.forEach(n => { tc[n.type] = (tc[n.type] || 0) + 1; });
      root.querySelector('.fv-stats').innerHTML =
        `Visible: ${vn.length} nodes, ${commE.length} comm + ${containE.length} contain edges — ` +
        Object.entries(tc).map(([k, v]) => k + ':' + v).join(' ');
    }

    function renderTree(vn, commE, containE, vnMap) {
      const cLink = g.append('g').selectAll('path').data(containE).enter().append('path')
        .attr('class', 'link contains').attr('stroke-width', 1)
        .attr('marker-end', 'url(#fv-arr-c)')
        .attr('d', d => { const sn = vnMap[d.source], dn = vnMap[d.target]; return sn && dn ? straightPath(sn.x, sn.y, dn.x, dn.y) : ''; });
      const hLink = g.append('g').selectAll('path').data(commE).enter().append('path')
        .attr('class', d => `link ${d.nature}${d.ext ? ' external' : ''}`)
        .attr('stroke-width', d => Math.min(1 + Math.log2(d.cnt + 1), 5))
        .attr('marker-end', 'url(#fv-arr)')
        .attr('d', d => { const sn = vnMap[d.source], dn = vnMap[d.target]; return sn && dn ? orthoPath(sn.x, sn.y, dn.x, dn.y) : ''; });
      const allLinks = g.selectAll('.link');
      const node = g.append('g').selectAll('g').data(vn, d => d.id).enter().append('g')
        .attr('class', nodeClass)
        .attr('transform', d => `translate(${d.x},${d.y})`)
        .call(d3.drag().on('drag', (e, d) => {
          d.x = e.x; d.y = e.y;
          d3.select(e.sourceEvent.target.parentNode).attr('transform', `translate(${d.x},${d.y})`);
          cLink.attr('d', ed => { const sn = vnMap[ed.source], dn = vnMap[ed.target]; return sn && dn ? straightPath(sn.x, sn.y, dn.x, dn.y) : ''; });
          hLink.attr('d', ed => { const sn = vnMap[ed.source], dn = vnMap[ed.target]; return sn && dn ? orthoPath(sn.x, sn.y, dn.x, dn.y) : ''; });
        }));
      node.append('circle').attr('r', d => R[d.type] || 8);
      node.append('text').attr('dy', d => (R[d.type] || 8) + 13).text(labelText);
      bindEvents(node, allLinks, [...commE, ...containE]);
    }

    function renderForce(vn, commE, containE, vnMap) {
      const levelY = {CN: H * 0.08, EX: H * 0.2, N: H * 0.38, E: H * 0.58, F: H * 0.78};
      vn.forEach(n => {
        n.x = W / 2 + (Math.random() - 0.5) * 500;
        n.y = (levelY[n.type] || H / 2) + (Math.random() - 0.5) * 60;
        delete n.fx; delete n.fy;
      });
      const commData = commE.map(e => ({...e, linkType: 'comm'}));
      const containData = containE.map(e => ({...e, linkType: 'contain'}));
      const cLink = g.append('g').selectAll('path').data(containData).enter().append('path')
        .attr('class', 'link contains').attr('stroke-width', 1);
      const hLink = g.append('g').selectAll('path').data(commData).enter().append('path')
        .attr('class', d => `link ${d.nature}${d.ext ? ' external' : ''}`)
        .attr('stroke-width', d => Math.min(1 + Math.log2(d.cnt + 1), 5))
        .attr('marker-end', 'url(#fv-arr-f)');
      const allLinks = g.selectAll('.link');
      const node = g.append('g').selectAll('g').data(vn, d => d.id).enter().append('g')
        .attr('class', nodeClass);
      node.append('circle').attr('r', d => R[d.type] || 8);
      node.append('text').attr('dy', d => (R[d.type] || 8) + 13).text(labelText);
      simulation = d3.forceSimulation(vn)
        .force('link', d3.forceLink([...commData, ...containData]).id(d => d.id)
          .distance(d => d.linkType === 'contain' ? 80 : 140)
          .strength(d => d.linkType === 'contain' ? 1.0 : 0.4))
        .force('charge', d3.forceManyBody().strength(-350))
        .force('center', d3.forceCenter(W / 2, H / 2))
        .force('collide', d3.forceCollide().radius(d => (R[d.type] || 8) + 18))
        .force('y', d3.forceY().y(d => levelY[d.type] || H / 2).strength(0.06))
        .on('tick', () => {
          node.attr('transform', d => `translate(${d.x},${d.y})`);
          hLink.attr('d', forceLinkPath); cLink.attr('d', forceLinkPath);
        });
      node.call(d3.drag()
        .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
        .on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }));
      bindEvents(node, allLinks, [...commE, ...containE]);
    }

    function bindEvents(node, allLinks, allEdges) {
      const tt = root.querySelector('.fv-tooltip');
      const eid = (x) => typeof x === 'object' ? x.id : x;

      node.on('click', (ev, d) => {
        ev.stopPropagation();
        if (mode === 'expand' && childrenOf[d.id] && !d.external) {
          if (exp.has(d.id)) collapse(d.id); else exp.add(d.id);
          rebuild();
        } else {
          showInfo(d, allEdges);
          highlightNeighbors(d, node, allLinks, allEdges);
        }
      });
      node.on('contextmenu', (ev, d) => {
        ev.preventDefault(); ev.stopPropagation();
        if (mode === 'expand') {
          if (exp.has(d.id)) { collapse(d.id); rebuild(); }
          else { const p = parentOf[d.id]; if (p !== undefined && exp.has(p)) { collapse(p); rebuild(); } }
        }
      });
      node.on('mouseenter', (ev, d) => {
        const ch = childrenOf[d.id] || [];
        let txt = `[${d.type}] ${d.full_name || d.label || d.id}`;
        if (d.executor_type) { txt += `  (${d.executor_type}`; if (d.num_threads) txt += `, ${d.num_threads} thr`; txt += ')'; }
        if (d.callback_group) txt += `  [${d.callback_group.type === 'MutuallyExclusive' ? 'MX' : 'RE'}]`;
        if (mode === 'expand' && ch.length && !d.external) {
          txt += exp.has(d.id) ? ' — click to collapse' : ` — click to expand (${ch.length})`;
        }
        tt.textContent = txt; tt.style.display = 'block';
        tt.style.left = (ev.offsetX + 12) + 'px'; tt.style.top = (ev.offsetY - 10) + 'px';
      }).on('mousemove', ev => {
        tt.style.left = (ev.offsetX + 12) + 'px'; tt.style.top = (ev.offsetY - 10) + 'px';
      }).on('mouseleave', () => tt.style.display = 'none');

      allLinks.on('mouseenter', (ev, d) => {
        if (d.nature === 'contains') {
          const sn = byId[eid(d.source)], tn = byId[eid(d.target)];
          tt.textContent = `contains: [${sn?.type}] ${(sn?.label||'?').split('/').pop()} → [${tn?.type}] ${(tn?.label||'?').split('/').pop()}`;
        } else {
          const topics = d.topics || [];
          const t = topics.length <= 3 ? topics.join(', ') : topics.slice(0, 3).join(', ') + ` +${topics.length - 3}`;
          tt.textContent = `${d.nature} (${d.cnt} edges): ${t || '?'}`;
        }
        tt.style.display = 'block';
        tt.style.left = (ev.offsetX + 12) + 'px'; tt.style.top = (ev.offsetY - 10) + 'px';
      }).on('mousemove', ev => {
        tt.style.left = (ev.offsetX + 12) + 'px'; tt.style.top = (ev.offsetY - 10) + 'px';
      }).on('mouseleave', () => tt.style.display = 'none');

      svg.on('click', () => {
        node.classed('dim', false); allLinks.classed('dim', false);
        root.querySelector('.fv-info').innerHTML =
          '<h3>FISH Graph</h3><p style="color:#666;font-size:12px"><b>Click</b> to expand/select. <b>Right-click</b> to collapse.</p>';
      });
    }

    function highlightNeighbors(d, node, allLinks, allEdges) {
      const eid = (x) => typeof x === 'object' ? x.id : x;
      const connected = new Set([d.id]);
      allEdges.forEach(e => {
        const s = eid(e.source), t = eid(e.target);
        if (s === d.id) connected.add(t);
        if (t === d.id) connected.add(s);
      });
      node.classed('dim', n => !connected.has(n.id));
      allLinks.classed('dim', e => {
        const s = eid(e.source), t = eid(e.target);
        return s !== d.id && t !== d.id;
      });
    }

    function showInfo(d, allEdges) {
      const eid = (x) => typeof x === 'object' ? x.id : x;
      let h = `<h3>[${d.type}] ${d.label || d.id}</h3>`;
      h += `<div class="fv-field"><span class="fv-key">ID:</span> <span class="fv-val">${d.id}</span></div>`;
      if (d.pid) h += `<div class="fv-field"><span class="fv-key">PID:</span> <span class="fv-val">${d.pid}</span></div>`;
      if (d.full_name) h += `<div class="fv-field"><span class="fv-key">Full name:</span> <span class="fv-val">${d.full_name}</span></div>`;
      if (d.etype) h += `<div class="fv-field"><span class="fv-key">Entity type:</span> <span class="fv-val">${d.etype}</span></div>`;
      if (d.ptype) h += `<div class="fv-field"><span class="fv-key">Func type:</span> <span class="fv-val">${d.ptype}</span></div>`;
      if (d.external) h += `<div class="fv-field"><span class="fv-key">External:</span> <span class="fv-val">yes</span></div>`;
      const ch = childrenOf[d.id] || [];
      if (ch.length) h += `<div class="fv-field"><span class="fv-key">Children:</span> <span class="fv-val">${ch.length}</span></div>`;
      if (d.aspects && d.aspects.length) {
        h += `<div class="fv-field"><span class="fv-key">Aspects (${d.aspects.length}):</span></div>`;
        d.aspects.slice(0, 20).forEach(a => {
          h += `<div class="fv-field" style="margin-left:10px"><span class="fv-val">${a.aspect}: ${a.topic || a.service || ''}</span></div>`;
        });
        if (d.aspects.length > 20) h += `<div class="fv-field" style="margin-left:10px;color:#888">+${d.aspects.length - 20} more</div>`;
      }
      const inc = [], out = [];
      allEdges.forEach(e => {
        const s = eid(e.source), t = eid(e.target);
        if (t === d.id && e.nature !== 'contains') inc.push({from: byId[s], e});
        if (s === d.id && e.nature !== 'contains') out.push({to: byId[t], e});
      });
      if (inc.length) {
        h += `<div class="fv-field"><span class="fv-key">← Incoming (${inc.length}):</span></div>`;
        inc.slice(0, 15).forEach(({from, e}) => {
          const lbl = from ? (from.label || '').split('/').pop() : '?';
          h += `<div class="fv-field" style="margin-left:10px"><span class="fv-val">${e.nature} from [${from?.type}] ${lbl} (${e.cnt})</span></div>`;
        });
      }
      if (out.length) {
        h += `<div class="fv-field"><span class="fv-key">→ Outgoing (${out.length}):</span></div>`;
        out.slice(0, 15).forEach(({to, e}) => {
          const lbl = to ? (to.label || '').split('/').pop() : '?';
          h += `<div class="fv-field" style="margin-left:10px"><span class="fv-val">${e.nature} to [${to?.type}] ${lbl} (${e.cnt})</span></div>`;
        });
      }
      root.querySelector('.fv-info').innerHTML = h;
    }

    // ── Controls (scoped to root) ──
    root.querySelectorAll('.fv-tabs button').forEach(btn => {
      btn.onclick = () => {
        root.querySelectorAll('.fv-tabs button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        mode = btn.dataset.mode;
        root.querySelector('.fv-panel-expand').style.display = mode === 'expand' ? '' : 'none';
        root.querySelector('.fv-panel-layer').style.display = mode === 'layer' ? '' : 'none';
        if (mode === 'expand') exp.clear();
        rebuild();
      };
    });
    root.querySelectorAll('.fv-depthBtns button').forEach(btn => {
      btn.onclick = () => {
        root.querySelectorAll('.fv-depthBtns button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        layerDepth = +btn.dataset.depth;
        if (layerDepth >= 99) {
          showExt = true;
          root.querySelector('.fv-showExternal').checked = true;
        }
        rebuild();
      };
    });
    root.querySelectorAll('.fv-layoutBtns button').forEach(btn => {
      btn.onclick = () => {
        root.querySelectorAll('.fv-layoutBtns button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        curLayout = btn.dataset.layout;
        root.querySelector('.fv-stairOpts').style.display = curLayout === 'stair' ? '' : 'none';
        rebuild();
      };
    });
    root.querySelector('.fv-stairMode').onchange = function () { stairMode = this.value; rebuild(); };
    root.querySelector('.fv-collapseAll').onclick = () => { exp.clear(); rebuild(); };
    root.querySelector('.fv-showExternal').onchange = function () { showExt = this.checked; rebuild(); };
    root.querySelector('.fv-showLabels').onchange = function () { showLbl = this.checked; rebuild(); };
    root.querySelector('.fv-showContain').onchange = function () { showContain = this.checked; rebuild(); };

    rebuild();
  }
})();
