import { useMemo, useRef, useState, Suspense } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { Html } from "@react-three/drei";
import { EffectComposer, Bloom } from "@react-three/postprocessing";
import * as THREE from "three";
import { GLBoundary } from "./AgentMesh";

/* ──────────────────────────────────────────────────────────────────────────
 * components/three/NodeGraph.jsx — reusable dimensional graph scene.
 *
 * Renders REAL relationships the caller derived from persisted data:
 *   nodes: [{ id, label, color, size?, layer? }]
 *   edges: [{ from, to, weight? }]           (weight 0..1 → opacity/pulse)
 *   layout: "orbit"   — hubs near center, leaves on a golden-spiral shell
 *           "columns" — nodes grouped into layers along X (pipelines)
 *
 * Same performance contract as AgentMesh: lazy-loaded, dpr-capped, tiny
 * geometry, static under prefers-reduced-motion, honest fallback on GL
 * failure. Hovering a node reveals its real label — no invented data.
 * ────────────────────────────────────────────────────────────────────────── */

const prefersReduced = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

const GOLDEN = Math.PI * (3 - Math.sqrt(5));

function computePositions(nodes, edges, layout) {
  const pos = new Map();
  if (layout === "columns") {
    const layers = new Map();
    for (const n of nodes) {
      const l = n.layer ?? 0;
      if (!layers.has(l)) layers.set(l, []);
      layers.get(l).push(n);
    }
    const layerKeys = [...layers.keys()].sort((a, b) => a - b);
    const spanX = Math.max(4.2, layerKeys.length * 1.15);
    layerKeys.forEach((l, li) => {
      const members = layers.get(l);
      const x = layerKeys.length > 1 ? (li / (layerKeys.length - 1) - 0.5) * spanX : 0;
      members.forEach((n, i) => {
        const a = (i / Math.max(1, members.length)) * Math.PI * 2 + li * 0.6;
        const r = members.length === 1 ? 0 : 0.55 + Math.min(1.15, members.length * 0.09);
        pos.set(n.id, new THREE.Vector3(x, Math.sin(a) * r, Math.cos(a) * r));
      });
    });
    return pos;
  }
  // "orbit": degree-weighted golden-spiral shell — hubs pulled inward.
  const degree = new Map();
  for (const e of edges) {
    degree.set(e.from, (degree.get(e.from) || 0) + 1);
    degree.set(e.to, (degree.get(e.to) || 0) + 1);
  }
  const maxDeg = Math.max(1, ...degree.values());
  nodes.forEach((n, i) => {
    const t = nodes.length > 1 ? i / (nodes.length - 1) : 0.5;
    const y = 1 - t * 2;
    const rad = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = GOLDEN * i;
    const shell = 2.1 * (1 - 0.62 * ((degree.get(n.id) || 0) / maxDeg));
    pos.set(n.id, new THREE.Vector3(
      Math.cos(theta) * rad * shell, y * shell * 0.72, Math.sin(theta) * rad * shell,
    ));
  });
  return pos;
}

function GraphNode({ node, position, animate, onHover, hovered }) {
  const ref = useRef();
  const size = node.size ?? 0.09;
  useFrame(({ clock }) => {
    if (!animate || !ref.current) return;
    ref.current.position.y = position.y + Math.sin(clock.elapsedTime * 0.8 + position.x * 3) * 0.035;
  });
  return (
    <group ref={ref} position={position}>
      <mesh
        onPointerOver={(e) => { e.stopPropagation(); onHover(node.id); }}
        onPointerOut={() => onHover(null)}
      >
        <sphereGeometry args={[size, 16, 16]} />
        <meshStandardMaterial color={node.color} emissive={node.color}
          emissiveIntensity={hovered ? 2.6 : 1.4} toneMapped={false} />
      </mesh>
      <mesh scale={1.9}>
        <sphereGeometry args={[size, 10, 10]} />
        <meshBasicMaterial color={node.color} transparent opacity={hovered ? 0.22 : 0.1} toneMapped={false} />
      </mesh>
      {hovered && node.label && (
        <Html center distanceFactor={7} style={{ pointerEvents: "none" }}>
          <div style={{
            transform: "translateY(-26px)", whiteSpace: "nowrap",
            background: "rgba(12,16,24,.92)", border: `1px solid ${node.color}`,
            borderRadius: 7, padding: "4px 9px", fontSize: 11.5,
            color: "#e8edf6", fontFamily: "'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif",
            boxShadow: "0 6px 24px rgba(2,6,12,.6)",
          }}>
            {node.label}
          </div>
        </Html>
      )}
    </group>
  );
}

function GraphEdge({ a, b, color, weight = 0.5 }) {
  const geom = useMemo(() => new THREE.BufferGeometry().setFromPoints([a, b]), [a, b]);
  return (
    <line geometry={geom}>
      <lineBasicMaterial color={color} transparent opacity={0.1 + weight * 0.32} toneMapped={false} />
    </line>
  );
}

function EdgePulse({ a, b, color, index, animate }) {
  const ref = useRef();
  useFrame(({ clock }) => {
    if (!animate || !ref.current) return;
    const t = (clock.elapsedTime * 0.22 + index * 0.31) % 1;
    ref.current.position.lerpVectors(a, b, t);
  });
  if (!animate) return null;
  return (
    <mesh ref={ref} position={a}>
      <sphereGeometry args={[0.028, 8, 8]} />
      <meshBasicMaterial color={color} toneMapped={false} />
    </mesh>
  );
}

function GraphScene({ nodes, edges, layout, animate }) {
  const group = useRef();
  const [hovered, setHovered] = useState(null);
  const positions = useMemo(() => computePositions(nodes, edges, layout), [nodes, edges, layout]);
  const nodeById = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  useFrame(({ pointer }, dt) => {
    if (!group.current) return;
    if (animate && !hovered) group.current.rotation.y += dt * (layout === "columns" ? 0.04 : 0.08);
    group.current.rotation.x = THREE.MathUtils.lerp(group.current.rotation.x, pointer.y * -0.14, 0.04);
  });

  // Only the strongest edges get pulses — motion communicates weight.
  const pulseEdges = useMemo(
    () => [...edges].sort((x, y) => (y.weight ?? 0) - (x.weight ?? 0)).slice(0, 10),
    [edges],
  );

  return (
    <group ref={group}>
      {edges.map((e, i) => {
        const a = positions.get(e.from), b = positions.get(e.to);
        if (!a || !b) return null;
        const color = nodeById.get(e.to)?.color || "#5b9dff";
        return <GraphEdge key={`e${i}`} a={a} b={b} color={color} weight={e.weight ?? 0.5} />;
      })}
      {pulseEdges.map((e, i) => {
        const a = positions.get(e.from), b = positions.get(e.to);
        if (!a || !b) return null;
        return <EdgePulse key={`p${i}`} a={a} b={b} color={nodeById.get(e.to)?.color || "#5b9dff"} index={i} animate={animate} />;
      })}
      {nodes.map((n) => {
        const p = positions.get(n.id);
        if (!p) return null;
        return <GraphNode key={n.id} node={n} position={p} animate={animate}
          onHover={setHovered} hovered={hovered === n.id} />;
      })}
    </group>
  );
}

export default function NodeGraph({ nodes = [], edges = [], layout = "orbit", height = 300 }) {
  const animate = !prefersReduced();
  const fallback = (
    <div style={{ height, background: "radial-gradient(circle at 50% 45%, rgba(91,157,255,.15), transparent 60%)" }} aria-hidden="true" />
  );
  if (!nodes.length) return fallback;

  return (
    <GLBoundary fallback={fallback}>
      <div style={{ height, position: "relative" }} role="img" aria-label="Relationship graph visualization">
        <Canvas
          dpr={[1, 1.75]}
          camera={{ position: [0, 0.7, layout === "columns" ? 5.2 : 4.6], fov: 42 }}
          gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
          frameloop={animate ? "always" : "demand"}
          style={{ background: "transparent" }}
        >
          <ambientLight intensity={0.55} />
          <pointLight position={[4, 5, 4]} intensity={36} color="#bcd6ff" />
          <Suspense fallback={null}>
            <GraphScene nodes={nodes} edges={edges} layout={layout} animate={animate} />
            {animate && (
              <EffectComposer multisampling={0}>
                <Bloom mipmapBlur intensity={0.75} luminanceThreshold={0.25} luminanceSmoothing={0.7} />
              </EffectComposer>
            )}
          </Suspense>
        </Canvas>
      </div>
    </GLBoundary>
  );
}
