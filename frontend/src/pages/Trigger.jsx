import { useState } from "react";

export default function Trigger() {
  const [form, setForm] = useState({
    event_type: "",
    metric: "",
    value: "",
    severity: "",
  });
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState(null);

  const handleChange = (e) => {
    setForm({ ...form, [e.target.name]: e.target.value });
  };

  const handleSubmit = async () => {
    setSubmitted(false);
    setError(null);
    try {
      await fetch("/api/v1/trigger/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, value: parseFloat(form.value) }),
      });
      setSubmitted(true);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div>
      <h2>Trigger Event</h2>
      <div>
        <label>Event Type</label><br />
        <input name="event_type" value={form.event_type} onChange={handleChange} />
      </div>
      <div>
        <label>Metric</label><br />
        <input name="metric" value={form.metric} onChange={handleChange} />
      </div>
      <div>
        <label>Value</label><br />
        <input name="value" type="number" value={form.value} onChange={handleChange} />
      </div>
      <div>
        <label>Severity</label><br />
        <input name="severity" value={form.severity} onChange={handleChange} />
      </div>
      <button onClick={handleSubmit}>Trigger</button>
      {submitted && <p>✓ Event triggered successfully.</p>}
      {error    && <p>✕ Error: {error}</p>}
    </div>
  );
}