/* MeshChat – Leaflet map with QWebChannel bridge */
"use strict";

var map, clusterGroup, bridge;
var nodes = {};          // node_num → { marker, data }

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
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
  });
  map.addLayer(clusterGroup);
} else {
  clusterGroup = L.layerGroup().addTo(map);
}

// ── Marker helpers ─────────────────────────────────────────────────────────
function markerIcon(data) {
  var cssClass = "mesh-marker-relayed";
  var size = 12;
  if (data.is_local) {
    cssClass = "mesh-marker-local";
    size = 16;
  } else if (data.hops_used === 0) {
    cssClass = "mesh-marker-direct";
    size = 14;
  } else if (data.last_heard_s !== null && data.last_heard_s > 3600) {
    cssClass = "mesh-marker-stale";
  }
  return L.divIcon({
    className: cssClass,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -(size / 2 + 2)],
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
    "<div class='meta'>Node " + data.node_num + (data.role ? "  ·  " + data.role : "") + "</div>" +
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
    existing.data = data;
  } else {
    var marker = L.marker([data.lat, data.lon], { icon: markerIcon(data) });
    marker.bindPopup(buildPopup(data));
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
  updateNodeCount();
}

function fitAll() {
  var latlngs = Object.values(nodes).map(function(n) {
    return n.marker.getLatLng();
  });
  console.log("fitAll: " + latlngs.length + " marker(s) to fit");
  if (latlngs.length === 0) return;
  if (latlngs.length === 1) {
    map.setView(latlngs[0], 13);
  } else {
    map.fitBounds(L.latLngBounds(latlngs).pad(0.1));
  }
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
