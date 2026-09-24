import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { Activity, ArrowRight, Check, CircleAlert, Inbox, LogOut, MessageSquare, Plus, RefreshCw, Search, ShieldAlert, ShieldCheck, UserCog, UserRound, Gavel, AlertTriangle, Clock, CheckCircle2, XCircle, FileText, Eye, ChevronLeft, ChevronRight, Trash2, BarChart3, Gauge, Timer, Star, TrendingUp, Users, Hash } from "lucide-react";
import "./styles.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function apiRequest(path, options = {}, token = null) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API_URL}${path}`, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    throw new Error(detail || "Something went wrong");
  }
  return body;
}

function slaMinutesLeft(ticket) {
  if (!ticket.sla_due_at) return Infinity;
  return (new Date(ticket.sla_due_at) - new Date()) / (1000 * 60);
}

function slaStatus(ticket) {
  if (!ticket.sla_due_at) return { label: "No SLA", class: "none" };
  const minutes = slaMinutesLeft(ticket);
  if (minutes < 0) return { label: "Overdue", class: "overdue" };
  if (minutes < 120) return { label: `${Math.round(minutes)}m left`, class: "critical" };
  if (minutes < 1440) return { label: `${Math.round(minutes / 60)}h left`, class: "warning" };
  return { label: `${Math.round(minutes / 1440)}d left`, class: "ok" };
}

function isTicketSlaOverdue(ticket) {
  return slaStatus(ticket).class === "overdue";
}

function AuthScreen({ onAuthenticated }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (mode === "register") {
        await apiRequest("/auth/register", { method: "POST", body: JSON.stringify({ email, password }) });
      }
      const result = await apiRequest("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      onAuthenticated(result);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-intro">
        <div className="brand-mark"><span>R</span></div>
        <p className="eyebrow">Relay / support operations</p>
        <h1>Make every customer feel heard.</h1>
        <p className="intro-copy">A calm, intelligent workspace for resolving the conversations that matter.</p>
        <div className="signal-row"><ShieldCheck size={17} /> Human oversight built into every escalation.</div>
      </section>
      <section className="auth-panel">
        <div className="auth-panel-top">
          <p className="eyebrow">Welcome back</p>
          <div className="mode-switch" role="tablist">
            <button className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>Sign in</button>
            <button className={mode === "register" ? "active" : ""} onClick={() => setMode("register")}>Create account</button>
          </div>
        </div>
        <h2>{mode === "login" ? "Enter your workspace" : "Start a support workspace"}</h2>
        <p className="muted">{mode === "login" ? "Sign in to continue to your queue." : "New accounts begin with customer access."}</p>
        <form onSubmit={submit} className="auth-form">
          <label>Email<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@company.com" required /></label>
          <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="At least 8 characters" minLength="8" required /></label>
          {error && <div className="error-box"><CircleAlert size={16} /> {error}</div>}
          <button className="primary-button" disabled={loading}>{loading ? "Working..." : mode === "login" ? "Open support desk" : "Create account"}<ArrowRight size={17} /></button>
        </form>
      </section>
    </main>
  );
}

function AdminTeamPanel({ session }) {
  const [users, setUsers] = useState([]);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("agent");
  const [notice, setNotice] = useState("");

  async function loadUsers() {
    try { setUsers(await apiRequest("/admin/users", {}, session.access_token)); }
    catch (error) { setNotice(error.message); }
  }

  useEffect(() => { loadUsers(); }, []);

  async function createUser(event) {
    event.preventDefault();
    try {
      await apiRequest("/admin/users", { method: "POST", body: JSON.stringify({ email, password, role }) }, session.access_token);
      setEmail(""); setPassword(""); setNotice("Team member created"); await loadUsers();
    } catch (error) { setNotice(error.message); }
  }

  async function toggleUser(user) {
    try {
      await apiRequest(`/admin/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !user.is_active }) }, session.access_token);
      await loadUsers();
    } catch (error) { setNotice(error.message); }
  }

  async function deleteUser(user) {
    if (user.id === session.user.id) return;
    if (!window.confirm(`Delete ${user.email}? This cannot be undone.`)) return;
    try {
      await apiRequest(`/admin/users/${user.id}`, { method: "DELETE" }, session.access_token);
      setNotice("User deleted"); await loadUsers();
    } catch (error) { setNotice(error.message); }
  }

  return (
    <section className="team-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Administration</p>
          <h2><UserCog size={19} /> Team access</h2>
          <p className="muted">Create and manage the people who work your support queue.</p>
        </div>
        <span>{users.length} users</span>
      </div>
      {notice && <div className="team-notice">{notice}</div>}
      <form className="team-form" onSubmit={createUser}>
        <div className="form-group">
          <label>Email<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="staff@company.com" required /></label>
        </div>
        <div className="form-group">
          <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Temporary password (min 8 chars)" minLength="8" required /></label>
        </div>
        <div className="form-group">
          <label>Role<select value={role} onChange={(event) => setRole(event.target.value)}><option value="agent">Agent</option><option value="admin">Admin</option></select></label>
        </div>
        <button className="secondary-button" type="submit"><Plus size={15} /> Add member</button>
      </form>
      <div className="team-list">
        <div className="team-list-header">
          <span className="col-user">User</span>
          <span className="col-role">Role</span>
          <span className="col-status">Status</span>
          <span className="col-actions">Actions</span>
        </div>
        {users.map((user) => (
          <div className="team-row" key={user.id}>
            <div className="col-user">
              <div className="avatar"><UserRound size={15} /></div>
              <div className="team-user"><strong>{user.email}</strong></div>
            </div>
            <div className="col-role"><span className={`role-badge ${user.role}`}>{user.role}</span></div>
            <div className="col-status"><span className={`account-state ${user.is_active ? "active" : "inactive"}`}>{user.is_active ? "Active" : "Inactive"}</span></div>
            <div className="col-actions">
              {user.id === session.user.id ? (
                <span className="current-badge">Current user</span>
              ) : (
                <>
                  <button className="team-toggle" onClick={() => toggleUser(user)}>{user.is_active ? "Deactivate" : "Activate"}</button>
                  <button className="team-toggle delete" onClick={() => deleteUser(user)}>Delete</button>
                </>
              )}
            </div>
          </div>
        ))}
        {users.length === 0 && <div className="empty-team">No team members yet. Add your first agent or admin above.</div>}
      </div>
    </section>
  );
}

function AuditActivity({ session }) {
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    apiRequest("/admin/audit-logs?limit=5", {}, session.access_token)
      .then(setLogs)
      .catch((requestError) => setError(requestError.message));
  }, []);

  return (
    <section className="audit-panel">
      <div className="team-heading"><div><p className="eyebrow">Traceability</p><h2><Activity size={19} /> Recent activity</h2><p className="muted">A record of important changes across the workspace.</p></div><span>{logs.length} recent events</span></div>
      {error ? <div className="team-notice error-text">{error}</div> : logs.length ? <div className="audit-list">{logs.map((log) => <article className="audit-row" key={log.id}><div className="audit-icon"><Activity size={14} /></div><div className="audit-copy"><strong>{log.action.replaceAll(".", " / ")}</strong><small>{log.entity_type} · {log.entity_id.slice(0, 8)} · {log.actor_id ? `by ${log.actor_id.slice(0, 8)}` : "system"}</small></div><time>{new Date(log.created_at).toLocaleString()}</time></article>)}</div> : <div className="empty-audit">No activity recorded yet.</div>}
    </section>
  );
}

function BreakdownBars({ items }) {
  const max = Math.max(...items.map((item) => item.count), 1);
  return (
    <div className="breakdown">
      {items.length ? items.map((item) => (
        <div className="breakdown-row" key={item.value}>
          <span className="breakdown-label" title={item.value}>{item.value || "unassigned"}</span>
          <div className="breakdown-track"><div className="breakdown-fill" style={{ width: item.count === 0 ? 0 : `${Math.max((item.count / max) * 100, 6)}%` }} /></div>
          <span className="breakdown-count">{item.count}</span>
        </div>
      )) : <div className="empty-audit">No data yet.</div>}
    </div>
  );
}

function StatCard({ icon, label, value, sub, tone }) {
  return (
    <article className={`analytics-stat ${tone || ""}`}>
      <div className="analytics-stat-icon">{icon}</div>
      <div className="analytics-stat-copy"><span>{label}</span><strong>{value}</strong>{sub && <small>{sub}</small>}</div>
    </article>
  );
}

function AnalyticsView({ session }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    apiRequest("/admin/analytics/dashboard", {}, session.access_token)
      .then(setData)
      .catch((requestError) => setError(requestError.message));
  }, [session.access_token]);

  if (error) return <section className="analytics-panel"><div className="team-notice error-text">{error}</div></section>;
  if (!data) return <section className="analytics-panel"><div className="empty-state"><BarChart3 size={28} /><strong>Loading analytics...</strong></div></section>;

  const { sla, csat, escalation, workload, channels, priorities, intents, sentiments, confidence } = data;
  const pct = (value) => (value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`);
  const ratingBars = [5, 4, 3, 2, 1].map((star) => ({ value: `${star} star`, count: csat.rating_distribution?.[String(star)] || 0 }));

  return (
    <section className="analytics-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Operations</p>
          <h2><BarChart3 size={19} /> Analytics</h2>
          <p className="muted">Service health, satisfaction and team load at a glance.</p>
        </div>
        <span>{new Date().toLocaleDateString()}</span>
      </div>

      <div className="analytics-stats">
        <StatCard icon={<Gauge size={17} />} label="Avg resolution" value={sla.average_resolution_hours === null ? "—" : `${sla.average_resolution_hours}h`} sub={`${sla.resolved_tickets} resolved of ${sla.total_tickets} total`} tone="teal" />
        <StatCard icon={<Timer size={17} />} label="Overdue SLA" value={sla.overdue_tickets} sub="open tickets past deadline" tone="danger" />
        <StatCard icon={<Star size={17} />} label="Avg rating" value={csat.average_rating === null ? "—" : `${csat.average_rating} / 5`} sub={`${csat.total_feedback} responses`} tone="amber" />
        <StatCard icon={<TrendingUp size={17} />} label="Deflection rate" value={pct(csat.deflection_rate)} sub={`response rate ${pct(csat.response_rate)}`} tone="teal" />
        <StatCard icon={<AlertTriangle size={17} />} label="Escalation rate" value={pct(escalation.escalation_rate)} sub={`${escalation.pending_review} pending review`} tone="danger" />
        <StatCard icon={<CircleAlert size={17} />} label="Needs review" value={escalation.pending_review} sub={`${escalation.approved} approved · ${escalation.rejected} rejected`} tone="amber" />
      </div>

      <div className="analytics-grid">
        <article className="analytics-card">
          <header><h4><Star size={15} /> CSAT distribution</h4></header>
          <BreakdownBars items={ratingBars} />
        </article>
        <article className="analytics-card">
          <header><h4><Activity size={15} /> Triage confidence</h4></header>
          <BreakdownBars items={[
            { value: "0.90+", count: confidence.very_high },
            { value: "0.80 - 0.89", count: confidence.high },
            { value: "0.70 - 0.79", count: confidence.medium },
            { value: "below 0.70", count: confidence.low },
          ]} />
          <p className="analytics-footnote">Average triage confidence: {confidence.average === null ? "—" : confidence.average.toFixed(2)}</p>
        </article>
        <article className="analytics-card">
          <header><h4><Users size={15} /> Team workload</h4></header>
          <BreakdownBars items={workload.map((entry) => ({ value: entry.assignee_id ? `agent ${entry.assignee_id.slice(0, 8)}` : "Unassigned", count: entry.assigned_tickets }))} />
        </article>
        <article className="analytics-card">
          <header><h4><Inbox size={15} /> Channels</h4></header>
          <BreakdownBars items={channels} />
        </article>
        <article className="analytics-card">
          <header><h4><Hash size={15} /> Priorities</h4></header>
          <BreakdownBars items={priorities} />
        </article>
        <article className="analytics-card">
          <header><h4><FileText size={15} /> Intents</h4></header>
          <BreakdownBars items={intents} />
        </article>
      </div>

      <article className="analytics-card">
        <header><h4><MessageSquare size={15} /> Customer sentiment</h4></header>
        <BreakdownBars items={sentiments} />
      </article>
    </section>
  );
}

function EscalationReviewPanel({ session }) {
  const [escalations, setEscalations] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [reviewReason, setReviewReason] = useState("");
  const [reviewAction, setReviewAction] = useState("");
  const [reviewLoading, setReviewLoading] = useState(false);

  const selectedEscalation = escalations.find((e) => e.id === selectedId) || null;

  async function loadEscalations() {
    setLoading(true);
    try {
      const result = await apiRequest("/tickets", {}, session.access_token);
      const pendingReview = result.filter((ticket) => ticket.escalation_status === "pending");
      setEscalations(pendingReview);
      if (!selectedId && pendingReview.length) setSelectedId(pendingReview[0].id);
    } catch (error) { setNotice(error.message); }
    finally { setLoading(false); }
  }

  useEffect(() => { loadEscalations(); }, []);

  async function handleReview(event) {
    event.preventDefault();
    if (!selectedEscalation || !reviewReason.trim() || !reviewAction) return;
    setReviewLoading(true);
    try {
      await apiRequest(`/tickets/${selectedEscalation.id}/escalation`, {
        method: "PATCH",
        body: JSON.stringify({ status: reviewAction, reason: reviewReason })
      }, session.access_token);
      setNotice(`Escalation ${reviewAction === "approved" ? "approved" : "rejected"}`);
      setReviewReason("");
      setReviewAction("");
      await loadEscalations();
    } catch (error) { setNotice(error.message); }
    finally { setReviewLoading(false); }
  }

  function formatDate(dateStr) {
    return new Date(dateStr).toLocaleString();
  }

  function getPriorityClass(priority) {
    const classes = { urgent: "urgent", high: "high", normal: "normal" };
    return classes[priority] || "normal";
  }

  function getSlaStatus(ticket) {
    if (!ticket.sla_due_at) return { label: "No SLA", class: "none" };
    const now = new Date();
    const due = new Date(ticket.sla_due_at);
    const hoursLeft = (due - now) / (1000 * 60 * 60);
    if (hoursLeft < 0) return { label: "OVERDUE", class: "overdue" };
    if (hoursLeft < 2) return { label: `${Math.round(hoursLeft * 60)}m left`, class: "critical" };
    if (hoursLeft < 24) return { label: `${Math.round(hoursLeft)}h left`, class: "warning" };
    return { label: `${Math.round(hoursLeft / 24)}d left`, class: "ok" };
  }

  return (
    <section className="escalation-review-panel">
      <div className="team-heading">
        <div>
          <p className="eyebrow">Human Review</p>
          <h2><Gavel size={19} /> Escalation queue</h2>
          <p className="muted">Tickets requiring human approval before automated response or closure.</p>
        </div>
        <span>{escalations.length} pending</span>
      </div>
      {notice && <div className="team-notice">{notice}</div>}
      <div className="review-grid">
        <div className="review-list-column">
          {loading ? (
            <div className="empty-state">Loading escalations...</div>
          ) : escalations.length ? (
            <div className="review-list">
              {escalations.map((esc) => (
                <button
                  key={esc.id}
                  className={`review-row ${esc.id === selectedId ? "selected" : ""}`}
                  onClick={() => setSelectedId(esc.id)}
                >
                  <div className="review-row-top">
                    <span className={`status-dot ${esc.escalation_status}`} />
                    <AlertTriangle size={16} className="escalation-icon" />
                    <strong>{esc.intent.replace("_", " ")}</strong>
                    <span className={`priority ${getPriorityClass(esc.priority)}`}>{esc.priority}</span>
                  </div>
                  <p>{esc.message.slice(0, 100)}...</p>
                  <small>{esc.customer_id} · {formatDate(esc.created_at)}</small>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <ShieldCheck size={28} />
              <strong>No pending escalations</strong>
              <span>All tickets are clear for automated handling.</span>
            </div>
          )}
        </div>
        <div className="review-detail-column">
          {selectedEscalation ? (
            <>
              <div className="detail-header">
                <div>
                  <span className="detail-kicker">Escalation {selectedEscalation.id.slice(0, 8)}</span>
                  <h2>{selectedEscalation.message}</h2>
                  <p className="muted">Created {formatDate(selectedEscalation.created_at)} · {selectedEscalation.channel}</p>
                </div>
                <span className={`priority large ${getPriorityClass(selectedEscalation.priority)}`}>{selectedEscalation.priority}</span>
              </div>
              <div className="detail-meta">
                <div><span>Intent</span><strong>{selectedEscalation.intent.replace("_", " ")}</strong></div>
                <div><span>Customer</span><strong>{selectedEscalation.customer_id}</strong></div>
                <div><span>Sentiment</span><strong>{selectedEscalation.sentiment}</strong></div>
                <div><span>Confidence</span><strong>{(selectedEscalation.confidence * 100).toFixed(0)}%</strong></div>
                <div><span>Recommended Team</span><strong>{selectedEscalation.recommended_team || "Unassigned"}</strong></div>
                <div><span>SLA Deadline</span><strong>{selectedEscalation.sla_due_at ? formatDate(selectedEscalation.sla_due_at) : "Not set"}</strong></div>
              </div>
              <div className="sla-badge">
                <Clock size={14} />
                <span className={`sla-${getSlaStatus(selectedEscalation).class}`}>{getSlaStatus(selectedEscalation).label}</span>
              </div>
              <div className="triage-summary">
                <h4><FileText size={16} /> Triage Summary</h4>
                <p>{selectedEscalation.triage_summary || "No summary available"}</p>
              </div>
              <div className="review-form" onSubmit={handleReview}>
                <h4><Gavel size={16} /> Review Decision</h4>
                <div className="review-options">
                  <label className="review-option">
                    <input type="radio" name="action" value="approved" checked={reviewAction === "approved"} onChange={() => setReviewAction("approved")} />
                    <div className="option-card approved">
                      <CheckCircle2 size={20} /> Approve
                      <small>Allow automated response or close ticket</small>
                    </div>
                  </label>
                  <label className="review-option">
                    <input type="radio" name="action" value="rejected" checked={reviewAction === "rejected"} onChange={() => setReviewAction("rejected")} />
                    <div className="option-card rejected">
                      <XCircle size={20} /> Reject
                      <small>Return to queue for manual handling</small>
                    </div>
                  </label>
                </div>
                <label>
                  Reason (required)
                  <textarea value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} placeholder="Explain your decision..." rows="3" required />
                </label>
                <button className="primary-button" type="submit" disabled={reviewLoading || !reviewAction || !reviewReason.trim()}>
                  {reviewLoading ? "Processing..." : `Submit ${reviewAction}`}
                  <ArrowRight size={16} />
                </button>
</div>
              <div className="intake-metadata">
                <h4><Eye size={16} /> Channel Metadata</h4>
                {selectedEscalation.intake_metadata && (
                  <pre className="metadata-display">{JSON.stringify(selectedEscalation.intake_metadata, null, 2)}</pre>
                )}
              </div>
            </>
          ) : (
            <div className="empty-detail">
              <Gavel size={28} />
              <strong>Select an escalation</strong>
              <span>Choose a ticket from the queue to review.</span>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function AppShell({ session, onLogout }) {
  const [tickets, setTickets] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [query, setQuery] = useState("");
  const [slaMode, setSlaMode] = useState("");
  const [newMessage, setNewMessage] = useState("");
  const [comment, setComment] = useState("");
  const [comments, setComments] = useState([]);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [activeView, setActiveView] = useState("inbox");
  const [expandTickets, setExpandTickets] = useState(false);

  const selectedTicket = tickets.find((ticket) => ticket.id === selectedId) || null;
  const isStaff = session.user.role !== "customer";

  async function loadTickets() {
    setLoading(true);
    try {
      const path = statusFilter ? `/tickets?status=${encodeURIComponent(statusFilter)}` : "/tickets";
      const result = await apiRequest(path, {}, session.access_token);
      setTickets(result);
      if (!selectedId && result.length) setSelectedId(result[0].id);
      if (selectedId && !result.some((ticket) => ticket.id === selectedId)) setSelectedId(result[0]?.id || null);
    } catch (error) { setNotice(error.message); }
    finally { setLoading(false); }
  }

  useEffect(() => { loadTickets(); }, [statusFilter, activeView]);

  useEffect(() => {
    if (!selectedTicket) { setComments([]); return; }
    apiRequest(`/tickets/${selectedTicket.id}/comments`, {}, session.access_token).then(setComments).catch((error) => setNotice(error.message));
  }, [selectedId]);

  useEffect(() => {
    if (activeView === "escalations" || !tickets.length) return;
    const matching = tickets.filter((ticket) =>
      activeView === "inbox" ? ticket.status !== "closed" :
      activeView === "conversations" ? ["resolved", "closed"].includes(ticket.status) :
      true
    );
    if (!matching.some((ticket) => ticket.id === selectedId)) setSelectedId(matching[0]?.id || null);
  }, [activeView]);

  async function createTicket(event) {
    event.preventDefault();
    if (!newMessage.trim()) return;
    try {
      const ticket = await apiRequest("/tickets", { method: "POST", body: JSON.stringify({ customer_id: session.user.id, message: newMessage, channel: "web" }) }, session.access_token);
      setNewMessage(""); setNotice("Ticket created"); await loadTickets(); setSelectedId(ticket.id);
    } catch (error) { setNotice(error.message); }
  }

  async function updateTicket(nextStatus) {
    try {
      await apiRequest(`/tickets/${selectedTicket.id}`, { method: "PATCH", body: JSON.stringify({ status: nextStatus }) }, session.access_token);
      setNotice("Ticket updated"); await loadTickets();
    } catch (error) { setNotice(error.message); }
  }

  async function addComment(event) {
    event.preventDefault();
    if (!comment.trim()) return;
    try {
      const created = await apiRequest(`/tickets/${selectedTicket.id}/comments`, { method: "POST", body: JSON.stringify({ author_id: session.user.id, body: comment, is_internal: isStaff }) }, session.access_token);
      setComments((current) => [...current, created]); setComment("");
    } catch (error) { setNotice(error.message); }
  }

  async function deleteTicket() {
    if (!selectedTicket || !window.confirm("Delete this ticket and all its messages? This cannot be undone.")) return;
    try {
      await apiRequest(`/tickets/${selectedTicket.id}`, { method: "DELETE" }, session.access_token);
      setNotice("Ticket deleted"); setSelectedId(null); await loadTickets();
    } catch (error) { setNotice(error.message); }
  }

  const viewTickets = tickets.filter((ticket) =>
    activeView === "inbox" ? ticket.status !== "closed" :
    activeView === "conversations" ? ["resolved", "closed"].includes(ticket.status) :
    true
  );
  const slaViewTickets = viewTickets.filter((ticket) => {
    if (ticket.status === "resolved" || ticket.status === "closed") return true;
    const minutes = slaMinutesLeft(ticket);
    if (slaMode === "overdue") return minutes < 0;
    if (slaMode === "soon") return minutes >= 0 && minutes <= 1440;
    return true;
  });
  const sortedViewTickets =
    slaMode === "urgency"
      ? [...slaViewTickets].sort((a, b) => {
          const aResolved = a.status === "resolved" || a.status === "closed";
          const bResolved = b.status === "resolved" || b.status === "closed";
          if (aResolved || bResolved) return aResolved === bResolved ? 0 : aResolved ? 1 : -1;
          if (slaMinutesLeft(a) === slaMinutesLeft(b)) return 0;
          return slaMinutesLeft(a) - slaMinutesLeft(b);
        })
      : slaViewTickets;
  const visibleTickets = sortedViewTickets.filter((ticket) => `${ticket.message} ${ticket.intent} ${ticket.customer_id}`.toLowerCase().includes(query.toLowerCase()));
  const shownTickets = expandTickets ? visibleTickets : visibleTickets.slice(0, 10);

return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand"><div className="brand-mark small"><span>R</span></div><strong>RESOLVE</strong></div>
        <div className="workspace-label">Workspace</div>
        <nav>
          <button className={`nav-item ${activeView === "inbox" ? "active" : ""}`} onClick={() => setActiveView("inbox")}><Inbox size={17} /> Inbox <span>{tickets.filter(t => t.status !== "closed").length}</span></button>
          {isStaff && <button className={`nav-item ${activeView === "escalations" ? "active" : ""}`} onClick={() => setActiveView("escalations")}><AlertTriangle size={17} /> Escalations <span>{tickets.filter(t => t.escalation_status === "pending").length}</span></button>}
          <button className={`nav-item ${activeView === "conversations" ? "active" : ""}`} onClick={() => setActiveView("conversations")}><MessageSquare size={17} /> Conversations <span>{tickets.filter(t => ["resolved", "closed"].includes(t.status)).length}</span></button>
          {session.user.role === "admin" && <button className={`nav-item ${activeView === "analytics" ? "active" : ""}`} onClick={() => setActiveView("analytics")}><BarChart3 size={17} /> Analytics</button>}
        </nav>
        <div className="sidebar-bottom"><div className="user-chip"><div className="avatar"><UserRound size={16} /></div><div><strong>{session.user.email.split("@")[0]}</strong><small>{session.user.role}</small></div></div><button className="logout-button" onClick={onLogout} title="Sign out"><LogOut size={17} /></button></div>
      </aside>
      <main className="workspace">
        <header className="topbar"><div><p className="eyebrow">{isStaff ? "Agent workspace" : "Customer portal"}</p><h1>{activeView === "escalations" ? "Escalation queue" : activeView === "conversations" ? "Conversation history" : activeView === "analytics" ? "Analytics" : isStaff ? "Support queue" : "Your conversations"}</h1></div><div className="topbar-actions"><span className="live-status"><span /> System healthy</span><button className="icon-button" onClick={loadTickets} title="Refresh tickets"><RefreshCw size={17} /></button></div></header>
        <section className="stats-row"><div><span>Open tickets</span><strong>{tickets.filter((ticket) => ticket.status === "open").length}</strong></div><div><span>Needs attention</span><strong>{tickets.filter((ticket) => ticket.requires_human_review).length}</strong></div><div><span>In progress</span><strong>{tickets.filter((ticket) => ticket.status === "in_progress").length}</strong></div></section>
        {notice && <div className="notice"><Check size={15} /> {notice}<button onClick={() => setNotice("")}>Dismiss</button></div>}
        {session.user.role === "admin" && <><AdminTeamPanel session={session} /><AuditActivity session={session} /></>}
        {activeView === "analytics" && session.user.role === "admin" ? (
          <AnalyticsView session={session} />
        ) : activeView === "escalations" && isStaff ? (
          <EscalationReviewPanel session={session} />
        ) : (
          <section className="desk-grid">
            <div className="ticket-column">
              <div className="toolbar"><div className="search-box"><Search size={16} /><input placeholder="Search tickets" value={query} onChange={(event) => setQuery(event.target.value)} /></div><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="">All statuses</option><option value="open">Open</option><option value="in_progress">In progress</option><option value="pending">Pending</option><option value="resolved">Resolved</option></select><select value={slaMode} onChange={(event) => setSlaMode(event.target.value)}><option value="">All deadlines</option><option value="overdue">Overdue only</option><option value="soon">Due within 24h</option><option value="urgency">SLA urgency</option></select></div>
              <div className="ticket-list">{loading ? <div className="empty-state">Loading queue...</div> : visibleTickets.length ? shownTickets.map((ticket) => { const sla = slaStatus(ticket); return <button className={`ticket-row ${ticket.id === selectedId ? "selected" : ""} ${isTicketSlaOverdue(ticket) ? "overdue" : ""}`} key={ticket.id} onClick={() => setSelectedId(ticket.id)}><div className="ticket-row-top"><span className={`status-dot ${ticket.status}`} /> <strong>{ticket.intent.replace("_", " ")}</strong>{isStaff && ticket.guardrail_status === "flagged" && <span className="guardrail-badge"><ShieldAlert size={12} /> flagged</span>}<span className={`priority ${ticket.priority}`}>{ticket.priority}</span></div><p>{ticket.message}</p><div className="ticket-row-meta"><span className={`sla-chip ${sla.class}`} title={ticket.sla_due_at ? `SLA due ${new Date(ticket.sla_due_at).toLocaleString()}` : "No SLA deadline"}><Clock size={12} /> {sla.label}</span><small>{ticket.customer_id} · {new Date(ticket.created_at).toLocaleDateString()}</small></div></button>; }) : <div className="empty-state"><Inbox size={28} /><strong>{activeView === "conversations" ? "No past conversations" : "No tickets here"}</strong><span>{activeView === "conversations" ? "Resolved and closed tickets will appear here." : "New conversations will appear in this queue."}</span></div>}{visibleTickets.length > 10 && <button className="show-more" onClick={() => setExpandTickets((value) => !value)}>{expandTickets ? "Show fewer" : `Show all ${visibleTickets.length} tickets`}</button>}</div>
              {!isStaff && <form className="new-ticket" onSubmit={createTicket}><label>Open a new conversation<textarea value={newMessage} onChange={(event) => setNewMessage(event.target.value)} placeholder="Tell us what happened..." rows="3" /></label><button className="secondary-button"><Plus size={16} /> Submit ticket</button></form>}
            </div>
            <div className="detail-column">{selectedTicket ? (
          <>
            <div className="detail-header"><div><span className="detail-kicker">Ticket {selectedTicket.id.slice(0, 8)}</span><h2>{selectedTicket.message}</h2><p className="muted">Created {new Date(selectedTicket.created_at).toLocaleString()} · {selectedTicket.channel}</p></div><div className="detail-header-actions"><span className={`priority large ${selectedTicket.priority}`}>{selectedTicket.priority}</span>{isStaff && <button className="trash-button" onClick={deleteTicket} title="Delete ticket"><Trash2 size={16} /></button>}</div></div>
            <div className="detail-meta"><div><span>Intent</span><strong>{selectedTicket.intent.replace("_", " ")}</strong></div><div><span>Customer</span><strong>{selectedTicket.customer_id}</strong></div><div><span>Review</span><strong>{selectedTicket.requires_human_review ? "Human review" : "Automated"}</strong></div><div><span>Guardrail</span><strong>{selectedTicket.guardrail_status === "flagged" ? `Flagged (${selectedTicket.guardrail_hits?.length || 0})` : "Clean"}</strong></div></div>
            {isStaff && selectedTicket.guardrail_hits?.length > 0 && (
              <div className="guardrail-flags">
                <h4><ShieldAlert size={15} /> Guardrail warnings</h4>
                {selectedTicket.guardrail_hits.map((hit, index) => (
                  <div className={`guardrail-flag ${hit.severity}`} key={index}>
                    <span className="guardrail-type">{hit.rule_type}</span>
                    <span className="guardrail-category">{hit.category}</span>
                    <code title={hit.description}>{hit.matched.slice(0, 40)}</code>
                  </div>
                ))}
              </div>
            )}
            {isStaff && <div className="status-actions"><span>Move ticket</span>{["open", "in_progress", "pending", "resolved", "closed"].map((status) => <button className={selectedTicket.status === status ? "active" : ""} key={status} onClick={() => updateTicket(status)}>{status.replace("_", " ")}</button>)}</div>}
            <div className="conversation"><div className="conversation-heading"><h3>Conversation</h3><span>{comments.length} messages</span></div>{comments.length ? comments.map((item) => <article className={`message ${item.is_internal ? "internal" : ""}`} key={item.id}><div className="message-avatar"><UserRound size={15} /></div><div><div className="message-meta"><strong>{item.author_id === session.user.id ? "You" : item.author_id}</strong>{item.is_internal && <span>Internal note</span>}<time>{new Date(item.created_at).toLocaleString()}</time></div><p>{item.body}</p></div></article>) : <div className="empty-conversation">No messages yet. Add the first reply.</div>}<form className="comment-form" onSubmit={addComment}><textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder={isStaff ? "Write an internal note..." : "Write a reply..."} rows="3" /><button className="primary-button">Send <ArrowRight size={16} /></button></form></div>
          </>
        ) : (
          <div className="empty-detail"><Inbox size={28} /><strong>Select a ticket</strong><span>Choose a conversation from the queue to view details.</span></div>
        )}</div>
          </section>
        )}
      </main>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState(() => JSON.parse(localStorage.getItem("relay-session") || "null"));
  const handleAuthenticated = (nextSession) => { localStorage.setItem("relay-session", JSON.stringify(nextSession)); setSession(nextSession); };
  const logout = () => { localStorage.removeItem("relay-session"); setSession(null); };
  return session ? <AppShell session={session} onLogout={logout} /> : <AuthScreen onAuthenticated={handleAuthenticated} />;
}

createRoot(document.getElementById("root")).render(<App />);