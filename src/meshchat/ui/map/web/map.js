/* MeshChat – Leaflet map with QWebChannel bridge */
"use strict";

var map, clusterGroup, bridge;
var nodes = {};          // node_num → { marker, data }
var selectedNodeNum = null;  // node_num currently highlighted, or null

// ── Map setup ──────────────────────────────────────────────────────────────
map = L.map("map", {
  center: [20, 0],
  zoom: 2,
  zoomControl: true,
  attributionControl: true,
});

// CARTO Dark Matter / Positron tiles — no API key, free for non-commercial use
var _attribution =
  "© <a href='https://www.openstreetmap.org/copyright' style='color:#00D4FF'>OpenStreetMap</a> contributors" +
  " &amp; © <a href='https://carto.com/attributions' style='color:#00D4FF'>CARTO</a>";

var darkTileLayer = L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  maxZoom: 19, subdomains: "abcd", attribution: _attribution,
});
var lightTileLayer = L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  maxZoom: 19, subdomains: "abcd", attribution: _attribution,
});

var _tilesLoaded = 0, _tileErrors = 0;
[darkTileLayer, lightTileLayer].forEach(function(layer) {
  layer.on("tileload", function() {
    _tilesLoaded++;
    if (_tilesLoaded === 1) console.log("Map: first basemap tile loaded successfully");
  });
  layer.on("tileerror", function(e) {
    _tileErrors++;
    if (_tileErrors <= 3) {
      console.error("Map: basemap tile failed to load — " + (e && e.error ? e.error.message || e.error : "unknown error"));
    }
  });
});

var activeTileLayer = darkTileLayer;
activeTileLayer.addTo(map);

function setMapTheme(theme) {
  var isLight = theme === "light";
  var wanted = isLight ? lightTileLayer : darkTileLayer;
  if (activeTileLayer !== wanted) {
    map.removeLayer(activeTileLayer);
    activeTileLayer = wanted;
    activeTileLayer.addTo(map);
  }
  document.body.classList.toggle("theme-light", isLight);
  document.body.classList.toggle("theme-dark", !isLight);
  var btn = document.getElementById("theme-toggle");
  if (btn) btn.textContent = isLight ? "🌙 Dark" : "☀ Light";
}

function toggleMapTheme() {
  var next = document.body.classList.contains("theme-light") ? "dark" : "light";
  setMapTheme(next);
  if (bridge && bridge.themeChanged) bridge.themeChanged(next);
}

// Cluster group
if (typeof L.markerClusterGroup !== "undefined") {
  clusterGroup = L.markerClusterGroup({
    showCoverageOnHover: false,
    // Keep the default (true). Nodes sharing the exact same lat/lon (e.g.
    // multiple radios at one fixed site) have 0px distance regardless of
    // zoom, so maxClusterRadius's zoom-based decluster below can't ever
    // separate them — spiderfying is the only way those pins/popups/clicks
    // stay reachable at max zoom. A previous attempt disabled this to avoid
    // a visual glitch (leg lines with no visible marker at the end); that
    // traded a real reachability bug for a cosmetic one, which is the wrong
    // side of that trade — leaving it on and revisiting the glitch
    // separately if it recurs.
    spiderfyOnMaxZoom: true,
    chunkedLoading: true,
    removeOutsideVisibleBounds: true,
    // Zoom-only, no dependency on live node count: leaflet.markercluster
    // reads this function once per zoom level, inside _generateInitialClusters()
    // — called only from onAdd() (map.addLayer(clusterGroup) below, while
    // `nodes` is still empty) and clearNodes()'s clearLayers(). A closure
    // reading Object.keys(nodes).length would freeze at whatever the count
    // was at that one-time call, not track the mesh growing afterward via
    // updateNode()/addLayer() — verified against the bundled
    // vendor/markercluster/leaflet.markercluster.js source. A static zoom
    // curve avoids that trap: real-world GPS points are virtually always
    // more than a few px apart by z15, which keeps on-screen circle count
    // reasonable without relying on a value that can't safely vary at runtime.
    maxClusterRadius: function(zoom) {
      return zoom >= 15 ? 1 : 50;
    },
  });
  map.addLayer(clusterGroup);
} else {
  clusterGroup = L.layerGroup().addTo(map);
}

// ── Marker helpers ─────────────────────────────────────────────────────────
function badgeLabel(data) {
  // Short enough to actually fit inside a small circle — first few
  // alphanumeric characters of the display name, upper-cased.
  var name = (data.name || "").replace(/[^A-Za-z0-9]/g, "");
  if (name) return name.slice(0, 4).toUpperCase();
  return "#" + (Math.abs(data.node_num) % 1000);
}

function markerIcon(data) {
  var cssClass = "mesh-marker-relayed";
  var size = 26;
  if (data.is_local) {
    cssClass = "mesh-marker-local";
    size = 32;
  } else if (data.hops_used === 0) {
    cssClass = "mesh-marker-direct";
    size = 30;
  } else if (data.last_heard_s !== null && data.last_heard_s > 3600) {
    cssClass = "mesh-marker-stale";
    size = 24;
  }
  if (data.node_num === selectedNodeNum) {
    cssClass += " mesh-marker-selected";
    size += 6;
  }
  return L.divIcon({
    className: cssClass,
    html: "<span>" + escapeHtml(badgeLabel(data)) + "</span>",
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -(size / 2 + 2)],
  });
}

function setSelectedNode(nodeNum) {
  var previous = selectedNodeNum;
  selectedNodeNum = nodeNum;
  // Only the previously- and newly-selected markers' appearance can have
  // changed — no need to touch every marker on the map.
  [previous, nodeNum].forEach(function(num) {
    var entry = nodes[num];
    if (entry) entry.marker.setIcon(markerIcon(entry.data));
  });
}

function buildPopup(data) {
  var hopsStr = data.hops_used !== null ? data.hops_used + " hop(s)" : "unknown hops";
  var ageStr  = data.last_heard_s !== null
    ? (data.last_heard_s < 60 ? data.last_heard_s + "s ago"
       : data.last_heard_s < 3600 ? Math.round(data.last_heard_s / 60) + "m ago"
       : Math.round(data.last_heard_s / 3600) + "h ago")
    : "—";

  return "<strong>" + escapeHtml(data.name) + "</strong>" +
    "<div class='meta'>Node " + data.node_num + (data.role ? "  ·  " + escapeHtml(data.role) : "") + "</div>" +
    "<div class='detail'>Last heard: " + ageStr + "</div>" +
    "<div class='detail'>Hops: " + hopsStr + "</div>" +
    "<a class='inspect-btn' onclick='nodeClicked(" + data.node_num + ")'>Inspect ↗</a>";
}

function escapeHtml(s) {
  if (!s) return "";
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function nodeClicked(nodeNum) {
  if (bridge) bridge.nodeClicked(nodeNum);
}

function labelText(data) {
  return data.name || ("!" + data.node_num.toString(16));
}

// ── Public API called from Python via page.runJavaScript ───────────────────
function updateNode(data) {
  if (!data || data.lat === null || data.lat === undefined || data.lon === null || data.lon === undefined) {
    console.warn("updateNode: rejected — missing lat/lon for node " + (data && data.node_num));
    return;
  }

  if (nodes[data.node_num]) {
    var existing = nodes[data.node_num];
    existing.marker.setLatLng([data.lat, data.lon]);
    existing.marker.setIcon(markerIcon(data));
    existing.marker.bindPopup(buildPopup(data));
    existing.marker.setTooltipContent(escapeHtml(labelText(data)));
    existing.data = data;
  } else {
    var marker = L.marker([data.lat, data.lon], { icon: markerIcon(data) });
    marker.bindPopup(buildPopup(data));
    // The badge itself already shows an abbreviated name (see markerIcon());
    // this tooltip is hover-only and shows the full name for disambiguation.
    marker.bindTooltip(escapeHtml(labelText(data)), {
      direction: "top",
      className: "mesh-marker-label",
      offset: [0, -8],
    });
    marker.on("click", function() { nodeClicked(data.node_num); });
    clusterGroup.addLayer(marker);
    nodes[data.node_num] = { marker: marker, data: data };
  }
  var count = Object.keys(nodes).length;
  if (count === 1 || count % 25 === 0) {
    console.log("updateNode: " + count + " node(s) on map now (clusterGroup has " + clusterGroup.getLayers().length + " layer(s))");
  }
  updateNodeCount();
}

function clearNodes() {
  clusterGroup.clearLayers();
  nodes = {};
  selectedNodeNum = null;  // otherwise orphaned until the next updateNode()
  updateNodeCount();
}

function median(values) {
  var sorted = values.slice().sort(function(a, b) { return a - b; });
  var mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function fitAll() {
  var latlngs = Object.values(nodes).map(function(n) {
    return n.marker.getLatLng();
  });
  console.log("fitAll: " + latlngs.length + " marker(s) to fit");
  if (latlngs.length === 0) return;
  if (latlngs.length === 1) {
    map.setView(latlngs[0], 13);
    return;
  }

  // Frame the bulk of the mesh, not the extremes. A single node reporting a
  // bogus position (sign-flipped longitude is a common GPS/config fault) or a
  // distant MQTT-bridged node would otherwise force a zoomed-out world view
  // that shows nothing useful. All markers stay on the map — this only
  // decides the initial framing.
  var lats = latlngs.map(function(p) { return p.lat; });
  var lons = latlngs.map(function(p) { return p.lng; });
  var medLat = median(lats), medLon = median(lons);

  var withDist = latlngs.map(function(p) {
    var dLat = p.lat - medLat, dLon = p.lng - medLon;
    return { p: p, d: Math.sqrt(dLat * dLat + dLon * dLon) };
  });
  var dists = withDist.map(function(x) { return x.d; })
                      .sort(function(a, b) { return a - b; });
  // Keep everything within the 90th percentile of distance-from-median
  var cutoff = dists[Math.floor(dists.length * 0.9)];
  var core = withDist.filter(function(x) { return x.d <= cutoff; })
                     .map(function(x) { return x.p; });

  var excluded = latlngs.length - core.length;
  if (excluded > 0) {
    console.log("fitAll: framing " + core.length + " node(s); " + excluded +
                " outlier(s) left off-view (still on the map)");
  }
  map.fitBounds(L.latLngBounds(core.length >= 2 ? core : latlngs).pad(0.1));
}

function refreshSize(refit) {
  // Leaflet caches the container size. The map lives on a tab that is hidden
  // at startup, so any fitBounds computed while hidden is against a zero-size
  // container and produces a nonsense view. Re-measure once the tab is shown.
  map.invalidateSize();
  if (refit) fitAll();
}

function focusNode(nodeNum) {
  var entry = nodes[nodeNum];
  if (!entry) {
    console.warn("focusNode: no marker for node " + nodeNum);
    return;
  }
  map.setView(entry.marker.getLatLng(), 15);
  entry.marker.openPopup();
}

function updateNodeCount() {
  var el = document.getElementById("node-count");
  if (el) el.textContent = Object.keys(nodes).length + " node(s)";
}

// ── QWebChannel setup ──────────────────────────────────────────────────────
if (typeof QWebChannel === "undefined") {
  console.error("QWebChannel class not loaded — vendor/qwebchannel.js failed to load");
} else if (typeof qt === "undefined") {
  console.error("qt.webChannelTransport not present — page was not loaded via QWebEngineView");
} else {
  console.log("QWebChannel: connecting...");
  new QWebChannel(qt.webChannelTransport, function(channel) {
    bridge = channel.objects.bridge;
    if (bridge && bridge.mapReady) {
      console.log("QWebChannel: bridge connected, calling mapReady()");
      bridge.mapReady();
    } else {
      console.error("QWebChannel: connected but 'bridge' object was not found in channel.objects");
    }
  });
}
