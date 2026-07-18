import { Component, useMemo, useRef, useState, Suspense } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { Html } from "@react-three/drei";
import { EffectComposer, Bloom } from "@react-three/postprocessing";
import * as THREE from "three";

/* ──────────────────────────────────────────────────────────────────────────
 * components/three/AgentMesh.jsx — the AEAM Enterprise Agent Mesh.
 *
 * A live, labelled architecture map of the REAL platform: the Orchestrator
 * core surrounded by every intelligence component that actually exists in
 * the backend, with evidence pulses travelling inward. Hovering a node
 * reveals its purpose plus REAL state/health/last-activity when the caller
 * supplies them (derived from persisted incidents + the Observability
 * engine — never invented; absent data reads "no recorded activity").
 *
 * Performance contract (unchanged): lazy-loaded, dpr-capped, tiny geometry,
 * rAF pauses with the tab, prefers-reduced-motion → static frame.
 * ────────────────────────────────────────────────────────────────────────── */

// The real component roster. Hex values mirror the CSS --c-* tokens.
export const ENGINES = [
  { key: "monitor",   label: "Monitor Agent",       color: "#38bdf8", purpose: "Watches live KPI feeds and turns anomalies into investigation events." },
  { key: "memory",    label: "Enterprise Memory",   color: "#a78bfa", purpose: "Recalls similar resolved incidents and reuses their outcomes as evidence." },
  { key: "policy",    label: "Policy Registry",     color: "#5b9dff", purpose: "Matches incidents against extracted enterprise policies — metric + semantic tiers." },
  { key: "cross",     label: "Cross-Dataset",       color: "#2dd4bf", purpose: "Correlates the incident metric against other activated business datasets." },
  { key: "adaptive",  label: "Adaptive Detection",  color: "#fbbf24", purpose: "Longer-horizon baselines and day-of-week seasonality checks." },
  { key: "retrieval", label: "Advanced Retrieval",  color: "#38bdf8", purpose: "Hybrid dense + lexical retrieval with reranking and business-relevance ranking." },
  { key: "plan",      label: "Execution Planning",  color: "#f472b6", purpose: "Synthesizes all evidence into one explainable, priority-ordered plan." },
  { key: "explain",   label: "Explainability",      color: "#c084fc", purpose: "Explains WHY each recommendation exists — evidence chains and confidence." },
  { key: "eval",      label: "AI Evaluation",       color: "#34d399", purpose: "Scores each investigation's thoroughness across ten transparent components." },
  { key: "observe",   label: "Observability",       color: "#94a3b8", purpose: "Cross-incident hit rates, trends and the platform's overall AI-health score." },
  { key: "report",    label: "Report Agent",        color: "#8fb3e8", purpose: "Generates the human-readable investigation report and audit summary." },
  { key: "action",    label: "Action Engine",       color: "#fb923c", purpose: "Executes the approved response — Slack, Jira, email, webhooks." },
];

const prefersReduced = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

function layout(radius) {
  return ENGINES.map((e, i) => {
    const a = (i / ENGINES.length) * Math.PI * 2;
    return {
      ...e,
      pos: new THREE.Vector3(
        Math.cos(a) * radius,
        Math.sin(a * 2.0) * radius * 0.2,
        Math.sin(a) * radius,
      ),
    };
  });
}

function Core({ color, animate, hovered }) {
  const wire = useRef();
  useFrame((_, dt) => {
    if (!animate || !wire.current) return;
    wire.current.rotation.x += dt * 0.22;
    wire.current.rotation.z += dt * 0.14;
  });
  return (
    <group>
      <mesh>
        <sphereGeometry args={[0.36, 24, 24]} />
        <meshStandardMaterial color={color} emissive={color} emissiveIntensity={2.4} toneMapped={false} />
      </mesh>
      <mesh ref={wire} scale={1.7}>
        <icosahedronGeometry args={[0.44, 1]} />
        <meshBasicMaterial color={color} wireframe transparent opacity={0.35} toneMapped={false} />
      </mesh>
      {!hovered && (
        <Html center distanceFactor={9} style={{ pointerEvents: "none" }}>
          <div style={{
            transform: "translateY(40px)", whiteSpace: "nowrap", textAlign: "center",
            fontSize: 12, fontWeight: 700, letterSpacing: ".16em", color: "#e8edf6",
            textShadow: "0 0 14px rgba(2,6,12,.9)", textTransform: "uppercase",
            fontFamily: "'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif",
          }}>
            Orchestrator
          </div>
        </Html>
      )}
    </group>
  );
}

function EngineNode({ node, index, animate, active, hoveredKey, onHover, info }) {
  const ref = useRef();
  const meshRef = useRef();
  const hovered = hoveredKey === node.key;
  const dimmed = hoveredKey && !hovered;

  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    if (animate && ref.current) {
      ref.current.position.y = node.pos.y + Math.sin(t * 0.9 + index * 1.7) * 0.055;
    }
    // Active agents pulse — emissive breathes; idle nodes hold steady.
    if (meshRef.current) {
      const base = hovered ? 2.8 : 1.5;
      meshRef.current.material.emissiveIntensity =
        active && animate ? base + Math.sin(t * 2.6 + index) * 0.7 : base;
    }
  });

  return (
    <group ref={ref} position={node.pos}>
      <mesh
        ref={meshRef}
        onPointerOver={(e) => { e.stopPropagation(); onHover(node.key); }}
        onPointerOut={() => onHover(null)}
      >
        <sphereGeometry args={[hovered ? 0.15 : 0.12, 18, 18]} />
        <meshStandardMaterial color={node.color} emissive={node.color} emissiveIntensity={1.5} toneMapped={false} />
      </mesh>
      <mesh scale={1.9}>
        <sphereGeometry args={[0.12, 12, 12]} />
        <meshBasicMaterial color={node.color} transparent opacity={hovered ? 0.24 : 0.11} toneMapped={false} />
      </mesh>

      {/* Always-on elegant label; fades back while another node is examined. */}
      <Html center distanceFactor={9} style={{ pointerEvents: "none" }} zIndexRange={[10, 0]}>
        <div style={{
          transform: "translateY(24px)", whiteSpace: "nowrap", textAlign: "center",
          fontSize: 11, fontWeight: 600, letterSpacing: ".02em",
          color: hovered ? "#ffffff" : "#b9c3d6",
          opacity: dimmed ? 0.25 : 1, transition: "opacity .2s, color .2s",
          textShadow: "0 0 12px rgba(2,6,12,.95), 0 0 4px rgba(2,6,12,.9)",
          fontFamily: "'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif",
        }}>
          {node.label}
        </div>
      </Html>

      {/* Hover: the agent dossier — real state only. */}
      {hovered && (
        <Html center distanceFactor={7} style={{ pointerEvents: "none" }} zIndexRange={[100, 90]}>
          <div style={{
            transform: "translateY(-86px)", width: 250, textAlign: "left",
            background: "rgba(12,16,24,.94)", border: `1px solid ${node.color}`,
            borderRadius: 10, padding: "10px 13px",
            boxShadow: `0 12px 40px rgba(2,6,12,.7), 0 0 24px ${node.color}33`,
            fontFamily: "'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif",
          }}>
            <div style={{ fontSize: 12.5, fontWeight: 700, color: "#fff", marginBottom: 4, display: "flex", alignItems: "center", gap: 7 }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: node.color, boxShadow: `0 0 8px ${node.color}` }} />
              {node.label}
            </div>
            <div style={{ fontSize: 11, color: "#b9c3d6", lineHeight: 1.5, marginBottom: 7 }}>{node.purpose}</div>
            <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 10px", fontSize: 10.5 }}>
              <span style={{ color: "#5d6679" }}>State</span>
              <span style={{ color: info?.state === "active" ? "#34d399" : "#b9c3d6" }}>{info?.state || "idle"}</span>
              <span style={{ color: "#5d6679" }}>Health</span>
              <span style={{ color: "#b9c3d6" }}>{info?.health ?? "not measured"}</span>
              <span style={{ color: "#5d6679" }}>Last activity</span>
              <span style={{ color: "#b9c3d6" }}>{info?.lastActivity || "no recorded activity"}</span>
            </div>
          </div>
        </Html>
      )}
    </group>
  );
}

function Edge({ from, to, color, boosted }) {
  const geom = useMemo(() => new THREE.BufferGeometry().setFromPoints([from, to]), [from, to]);
  return (
    <line geometry={geom}>
      <lineBasicMaterial color={color} transparent opacity={boosted ? 0.4 : 0.2} toneMapped={false} />
    </line>
  );
}

function Pulse({ node, index, animate, speed }) {
  const ref = useRef();
  useFrame(({ clock }) => {
    if (!animate || !ref.current) return;
    const t = (clock.elapsedTime * speed + index * 0.19) % 1;
    ref.current.position.lerpVectors(node.pos, ORIGIN, t);
    const s = 1 - Math.abs(t - 0.5) * 1.4;
    ref.current.scale.setScalar(Math.max(0.35, s));
  });
  if (!animate) return null;
  return (
    <mesh ref={ref} position={node.pos}>
      <sphereGeometry args={[0.035, 8, 8]} />
      <meshBasicMaterial color={node.color} toneMapped={false} />
    </mesh>
  );
}
const ORIGIN = new THREE.Vector3(0, 0, 0);

function Particles({ count, animate }) {
  const ref = useRef();
  const positions = useMemo(() => {
    const arr = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      const r = 3.4 + Math.random() * 3.6;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      arr[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      arr[i * 3 + 1] = r * Math.cos(phi) * 0.6;
      arr[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
    }
    return arr;
  }, [count]);
  useFrame((_, dt) => { if (animate && ref.current) ref.current.rotation.y -= dt * 0.008; });
  return (
    <points ref={ref}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" count={count} array={positions} itemSize={3} />
      </bufferGeometry>
      <pointsMaterial size={0.022} color="#7fa8d9" transparent opacity={0.5} sizeAttenuation />
    </points>
  );
}

function MeshScene({ coreColor, animate, radius, live, investigating }) {
  const group = useRef();
  const [hoveredKey, setHoveredKey] = useState(null);
  const nodes = useMemo(() => layout(radius), [radius]);
  // Investigations in flight → evidence flows faster.
  const pulseSpeed = investigating ? 0.4 : 0.22;

  useFrame(({ pointer }, dt) => {
    if (!group.current) return;
    if (animate && !hoveredKey) group.current.rotation.y += dt * 0.06;
    group.current.rotation.x = THREE.MathUtils.lerp(group.current.rotation.x, pointer.y * -0.14, 0.04);
    group.current.rotation.z = THREE.MathUtils.lerp(group.current.rotation.z, pointer.x * 0.045, 0.04);
  });

  return (
    <group ref={group}>
      <Core color={coreColor} animate={animate} hovered={!!hoveredKey} />
      {nodes.map((n, i) => {
        const info = live?.[n.key];
        const active = info?.state === "active";
        return (
          <group key={n.key}>
            <Edge from={n.pos} to={ORIGIN} color={n.color} boosted={active || hoveredKey === n.key} />
            <EngineNode node={n} index={i} animate={animate} active={active}
              hoveredKey={hoveredKey} onHover={setHoveredKey} info={info} />
            <Pulse node={n} index={i} animate={animate && (active || !live)} speed={pulseSpeed} />
          </group>
        );
      })}
    </group>
  );
}

export class GLBoundary extends Component {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return this.props.fallback;
    return this.props.children;
  }
}

/**
 * @param {number|null} health        0–1 AI-health score → core tint.
 * @param {"dashboard"|"welcome"} variant
 * @param {number|string} height
 * @param {object|null} live          {engineKey: {state, health, lastActivity}} — REAL agent activity.
 * @param {boolean} investigating     true when incidents are actively open (faster evidence flow).
 */
export default function AgentMesh({ health = null, variant = "dashboard", height = 300, live = null, investigating = false }) {
  const animate = !prefersReduced();
  const welcome = variant === "welcome";
  const coreColor =
    health == null ? "#5b9dff" : health >= 0.7 ? "#34d399" : health >= 0.4 ? "#fbbf24" : "#f87171";

  const fallback = (
    <div style={{
      height, borderRadius: "inherit",
      background: "radial-gradient(circle at 50% 45%, rgba(91,157,255,.18), transparent 60%)",
    }} aria-hidden="true" />
  );

  return (
    <GLBoundary fallback={fallback}>
      <div style={{ height, position: "relative" }} aria-label="AEAM agent mesh — live architecture map" role="img">
        <Canvas
          dpr={[1, 1.75]}
          camera={{ position: [0, 1.15, welcome ? 5.4 : 4.9], fov: 44 }}
          gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
          frameloop={animate ? "always" : "demand"}
          style={{ background: "transparent" }}
        >
          <ambientLight intensity={0.5} />
          <pointLight position={[4, 5, 4]} intensity={44} color="#bcd6ff" />
          {/* Cool rim light from behind-left for depth separation. */}
          <directionalLight position={[-6, 2, -4]} intensity={1.6} color="#2dd4bf" />
          <Suspense fallback={null}>
            <MeshScene coreColor={coreColor} animate={animate} radius={welcome ? 2.2 : 1.95}
              live={live} investigating={investigating} />
            <Particles count={welcome ? 460 : 240} animate={animate} />
            {animate && (
              <EffectComposer multisampling={0}>
                <Bloom mipmapBlur intensity={welcome ? 1.3 : 0.85} luminanceThreshold={0.22} luminanceSmoothing={0.65} />
              </EffectComposer>
            )}
          </Suspense>
        </Canvas>
      </div>
    </GLBoundary>
  );
}
