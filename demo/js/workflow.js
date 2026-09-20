/* The workflow graph: layout, SVG edges, and the pan/zoom canvas.

   The real page hands React Flow a node and edge list built by
   ``AdminService._graph``; this file builds the same lists from the demo's
   transitions and draws them itself, keeping the rules that matter — a
   forward arrow is green, a return arrow is dashed amber and detours under
   the row, a person's arrow is yellow, and a long forward hop routes over
   the top rather than through the nodes it would otherwise cross. */
(function (global) {
  const SPACING = 440;      // WORKFLOW_NODE_SPACING
  const NODE_W = 220;
  const NODE_H = 74;
  const LANE = 90;          // distance between detour lanes
  const LANE_OFFSET = 150;  // first lane's distance from the node row
  const RADIUS = 14;        // corner rounding on a detour
  const COLOR_FORWARD = "#167d5a";
  const COLOR_RETURN = "#d97706";
  const COLOR_HUMAN = "#d1a207";
  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 1.75;

  const esc = (value) =>
    String(value).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* Where each status sits, and what kind of node it is. */
  function layout(section) {
    const order = new Map();
    section.statuses.forEach((status, index) => order.set(status, index));
    const triggered = new Set(Object.keys(section.agents));
    const final = new Set(section.finalStatuses || []);
    const disconnected = section.statuses.length > 1 && section.transitions.length === 0;

    const nodes = section.statuses.map((status, index) => {
      const isHuman = !triggered.has(status) && !final.has(status);
      const run = section.agents[status];
      const classes = ["workflow-node"];
      if (disconnected) classes.push("workflow-node--disconnected");
      if (isHuman) classes.push("workflow-node--human");
      if (run) classes.push("workflow-node--agent");
      const tooltip = isHuman
        ? section.humanActions[status] ||
          "A person moves this status forward: no issue-trigger skill handles it."
        : run
        ? run.skill + "\n" + run.agent + " · " + (run.model || "agent default")
        : "";
      return {
        id: "status-" + index,
        status,
        x: index * SPACING,
        y: 0,
        className: classes.join(" "),
        model: run ? run.model || "agent default" : "",
        tooltip,
      };
    });

    let returnLane = 0;
    let forwardLane = 0;
    const edges = section.transitions.map((transition, index) => {
      const from = order.get(transition.source);
      const to = order.get(transition.target);
      const isReturn = to <= from;
      const isLongForward = to > from + 1;
      const color = transition.human
        ? COLOR_HUMAN
        : isReturn
        ? COLOR_RETURN
        : COLOR_FORWARD;
      const edge = {
        index,
        source: transition.source,
        target: transition.target,
        labels: transition.labels || [],
        reasons: transition.reasons || [],
        human: !!transition.human,
        isReturn,
        color,
        ariaLabel:
          transition.source + " to " + transition.target +
          (transition.labels && transition.labels.length
            ? " via " + transition.labels.join(", ")
            : transition.human ? " by a person" : ""),
      };
      if (isReturn) {
        edge.path = detour(from, to, LANE_OFFSET + NODE_H + returnLane * LANE, "below");
        returnLane += 1;
      } else if (isLongForward) {
        edge.path = detour(from, to, -(LANE_OFFSET + forwardLane * LANE), "above");
        forwardLane += 1;
      } else {
        edge.path = straight(from, to);
      }
      return edge;
    });

    return { nodes, edges, warnings: section.warnings || [] };
  }

  /* An arrow between neighbours: out of the right edge, into the left one. */
  function straight(from, to) {
    const y = NODE_H / 2;
    const x1 = from * SPACING + NODE_W;
    const x2 = to * SPACING;
    return {
      d: "M " + x1 + " " + y + " L " + x2 + " " + y,
      labelAt: { x: (x1 + x2) / 2, y },
    };
  }

  /* A hop that would otherwise cut through the nodes between its ends, routed
     out of the row and back in. `side` decides which way the arrow re-enters. */
  function detour(from, to, laneY, side) {
    const x1 = from * SPACING + NODE_W / 2;
    const x2 = to * SPACING + NODE_W / 2;
    const y1 = side === "below" ? NODE_H : 0;
    const y2 = side === "below" ? NODE_H : 0;
    const sweep = x2 > x1 ? 1 : -1;
    const dir = side === "below" ? 1 : -1;
    const r = Math.min(RADIUS, Math.abs(x2 - x1) / 2, Math.abs(laneY - y1));
    const d = [
      "M", x1, y1,
      "L", x1, laneY - dir * r,
      "Q", x1, laneY, x1 + sweep * r, laneY,
      "L", x2 - sweep * r, laneY,
      "Q", x2, laneY, x2, laneY - dir * r,
      "L", x2, y2,
    ].join(" ");
    return { d, labelAt: { x: (x1 + x2) / 2, y: laneY } };
  }

  function bounds(model) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    model.nodes.forEach((node) => {
      minX = Math.min(minX, node.x);
      minY = Math.min(minY, node.y);
      maxX = Math.max(maxX, node.x + NODE_W);
      maxY = Math.max(maxY, node.y + NODE_H);
    });
    model.edges.forEach((edge) => {
      const y = edge.path.labelAt.y;
      minY = Math.min(minY, y - 20);
      maxY = Math.max(maxY, y + 20);
    });
    return { minX: minX - 40, minY: minY - 40, maxX: maxX + 40, maxY: maxY + 40 };
  }

  function edgesSvg(model, box) {
    const colors = [...new Set(model.edges.map((edge) => edge.color))];
    const defs = colors
      .map(
        (color) =>
          '<marker id="arrow-' + color.slice(1) + '" viewBox="0 0 10 10" refX="9" refY="5" ' +
          'markerWidth="5" markerHeight="5" orient="auto-start-reverse">' +
          '<path d="M 0 0 L 10 5 L 0 10 z" fill="' + color + '"/></marker>'
      )
      .join("");

    const body = model.edges
      .map((edge) => {
        const label = edge.labels.join(", ");
        const at = edge.path.labelAt;
        const width = label.length * 6.6 + 12;
        const chip = label
          ? '<rect class="edge-label-bg" x="' + (at.x - width / 2) + '" y="' + (at.y - 11) +
            '" width="' + width + '" height="22"/>' +
            '<text class="edge-label" x="' + at.x + '" y="' + (at.y + 4) +
            '" text-anchor="middle">' + esc(label) + "</text>"
          : "";
        return (
          '<g class="edge-group' + (edge.human ? " is-human" : "") + '" data-edge="' + edge.index + '" ' +
          'style="color:' + edge.color + '" aria-label="' + esc(edge.ariaLabel) + '">' +
          '<path class="workflow-edge-path' + (edge.isReturn ? " is-return" : "") + '" d="' + edge.path.d +
          '" stroke="' + edge.color + '" marker-end="url(#arrow-' + edge.color.slice(1) + ')"/>' +
          '<path class="hit" d="' + edge.path.d + '" data-edge="' + edge.index + '"/>' +
          chip +
          "</g>"
        );
      })
      .join("");

    return (
      '<svg class="workflow-edges" width="' + (box.maxX - box.minX) + '" height="' + (box.maxY - box.minY) +
      '" viewBox="' + box.minX + " " + box.minY + " " + (box.maxX - box.minX) + " " + (box.maxY - box.minY) +
      '" style="left:' + box.minX + "px;top:" + box.minY + 'px"><defs>' + defs + "</defs>" + body + "</svg>"
    );
  }

  function nodesHtml(model) {
    return model.nodes
      .map(
        (node) =>
          '<div class="' + node.className + '" style="left:' + node.x + "px;top:" + node.y +
          "px;--codee-node-tooltip:'" + node.tooltip.replace(/'/g, "\\'").replace(/\n/g, "\\A ") + "'\">" +
          "<div>" + esc(node.status) + "</div>" +
          (node.model ? '<div class="model-line">' + esc(node.model) + "</div>" : "") +
          "</div>"
      )
      .join("");
  }

  /* --------------------------------------------------------- the canvas */

  function render(container, section, options) {
    const opts = options || {};
    const model = layout(section);
    const box = bounds(model);

    container.className = "workflow-canvas";
    container.innerHTML =
      '<div class="workflow-viewport">' +
      '<div class="workflow-dots"></div>' +
      edgesSvg(model, box) +
      nodesHtml(model) +
      "</div>" +
      '<div class="zoom-controls">' +
      '<button type="button" data-zoom="in" title="Zoom in" aria-label="Zoom in">' + global.icon("zoom-in", 15) + "</button>" +
      '<button type="button" data-zoom="out" title="Zoom out" aria-label="Zoom out">' + global.icon("zoom-out", 15) + "</button>" +
      '<button type="button" data-zoom="fit" title="Fit view" aria-label="Fit view">' + global.icon("maximize", 15) + "</button>" +
      "</div>";

    const viewport = container.querySelector(".workflow-viewport");
    const view = { x: 0, y: 0, k: 1 };

    function apply() {
      viewport.style.transform =
        "translate(" + view.x + "px," + view.y + "px) scale(" + view.k + ")";
    }

    /* Fit, but never below the zoom the node labels stop being readable at:
       a workflow of eight statuses is 3,500px wide, and shrinking it to fit
       turns the page into a diagram of grey bars. Past that floor the graph
       is left-aligned and the rest is a pan away. */
    const READABLE_ZOOM = 0.62;

    function fit() {
      const rect = container.getBoundingClientRect();
      const pad = 32;
      const w = box.maxX - box.minX;
      const h = box.maxY - box.minY;
      const k = Math.max(
        Math.min(READABLE_ZOOM, MAX_ZOOM),
        Math.min(MAX_ZOOM, (rect.width - pad * 2) / w, (rect.height - pad * 2) / h)
      );
      view.k = k;
      view.x = w * k + pad * 2 > rect.width ? pad - box.minX * k : (rect.width - w * k) / 2 - box.minX * k;
      view.y = h * k + pad * 2 > rect.height ? pad - box.minY * k : (rect.height - h * k) / 2 - box.minY * k;
      apply();
    }

    function zoomBy(factor, originX, originY) {
      const rect = container.getBoundingClientRect();
      const cx = originX === undefined ? rect.width / 2 : originX;
      const cy = originY === undefined ? rect.height / 2 : originY;
      const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, view.k * factor));
      const ratio = next / view.k;
      view.x = cx - (cx - view.x) * ratio;
      view.y = cy - (cy - view.y) * ratio;
      view.k = next;
      apply();
    }

    /* Pan. A drag that starts on an edge's hit area still pans: the click
       handler below only fires when the pointer barely moved. */
    let dragging = null;

    function onDragMove(event) {
      const dx = event.clientX - dragging.x;
      const dy = event.clientY - dragging.y;
      dragging.moved = Math.max(dragging.moved, Math.abs(dx) + Math.abs(dy));
      view.x = dragging.ox + dx;
      view.y = dragging.oy + dy;
      apply();
    }

    function onDragEnd() {
      container.dataset.dragged = dragging.moved > 4 ? "1" : "";
      dragging = null;
      container.classList.remove("is-panning");
      window.removeEventListener("mousemove", onDragMove);
      window.removeEventListener("mouseup", onDragEnd);
    }

    container.addEventListener("mousedown", (event) => {
      if (event.button !== 0 || event.target.closest(".zoom-controls")) return;
      dragging = { x: event.clientX, y: event.clientY, ox: view.x, oy: view.y, moved: 0 };
      container.classList.add("is-panning");
      window.addEventListener("mousemove", onDragMove);
      window.addEventListener("mouseup", onDragEnd);
    });

    container.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const rect = container.getBoundingClientRect();
        zoomBy(
          event.deltaY < 0 ? 1.12 : 1 / 1.12,
          event.clientX - rect.left,
          event.clientY - rect.top
        );
      },
      { passive: false }
    );

    container.querySelectorAll("[data-zoom]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const kind = button.dataset.zoom;
        if (kind === "fit") fit();
        else zoomBy(kind === "in" ? 1.2 : 1 / 1.2);
      });
    });

    /* Hover: light the whole transition, dim everything else, and say why the
       arrow is there next to the pointer. */
    const groups = [...container.querySelectorAll(".edge-group")];
    const byIndex = new Map(model.edges.map((edge) => [String(edge.index), edge]));

    groups.forEach((group) => {
      const edge = byIndex.get(group.dataset.edge);
      const hit = group.querySelector(".hit");
      hit.addEventListener("mouseenter", (event) => {
        container.classList.add("has-hover");
        group.classList.add("is-hovered");
        if (edge.reasons.length && opts.onEdgeHover) {
          opts.onEdgeHover(edge, event.clientX, event.clientY);
        }
      });
      hit.addEventListener("mousemove", (event) => {
        if (edge.reasons.length && opts.onEdgeHover) {
          opts.onEdgeHover(edge, event.clientX, event.clientY);
        }
      });
      hit.addEventListener("mouseleave", () => {
        container.classList.remove("has-hover");
        group.classList.remove("is-hovered");
        if (opts.onEdgeLeave) opts.onEdgeLeave();
      });
      hit.addEventListener("click", (event) => {
        if (container.dataset.dragged === "1") return;
        event.stopPropagation();
        if (!edge.labels.length) return;
        if (opts.onEdgeClick) opts.onEdgeClick(edge, event.clientX, event.clientY);
      });
    });

    container.addEventListener("click", (event) => {
      if (event.target.closest(".hit") || event.target.closest(".zoom-controls")) return;
      if (opts.onPaneClick) opts.onPaneClick();
    });

    requestAnimationFrame(fit);
    return { fit, model };
  }

  global.Workflow = { render, layout };
})(window);
