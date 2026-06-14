import { useEffect, useState } from "react";
import AgentLogCard from "../components/AgentLogCard";

export default function Agents() {
  const [logs, setLogs] = useState([]);

  useEffect(() => {
    fetch("http://localhost:8000/api/v1/logs/agents")
      .then(res => res.json())
      .then(data => setLogs(data))
      .catch(err => console.error(err));
  }, []);

  return (
    <div>
      <h2>Agent Logs</h2>
      {logs.map((log, i) => (
        <AgentLogCard key={i} log={log} />
      ))}
    </div>
  );
}