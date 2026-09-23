import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { Activity, ArrowRight, Check, CircleAlert, Inbox, LogOut, MessageSquare, Plus, RefreshCw, Search, ShieldCheck, UserCog, UserRound } from "lucide-react";
import "./styles.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function apiRequest(path, options = {}, token = null) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API_URL}${path}`, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "Something went wrong");
  return body;
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

  return (
    <section className="team-panel">
      <div className="team-heading"><div><p className="eyebrow">Administration</p><h2><UserCog size={19} /> Team access</h2><p className="muted">Create and manage the people who work your support queue.</p></div><span>{users.length} users</span></div>
      {notice && <div className="team-notice">{notice}</div>}
      <form className="team-form" onSubmit={createUser}><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="staff@company.com" required /><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Temporary password" minLength="8" required /><select value={role} onChange={(event) => setRole(event.target.value)}><option value="agent">Agent</option><option value="admin">Admin</option></select><button className="secondary-button"><Plus size={15} /> Add member</button></form>
      <div className="team-list">{users.map((user) => <div className="team-row" key={user.id}><div className="avatar"><UserRound size={15} /></div><div className="team-user"><strong>{user.email}</strong><small>{user.role}</small></div><span className={`account-state ${user.is_active ? "active" : "inactive"}`}>{user.is_active ? "Active" : "Inactive"}</span><button className="team-toggle" disabled={user.id === session.user.id} onClick={() => toggleUser(user)}>{user.id === session.user.id ? "Current user" : user.is_active ? "Deactivate" : "Activate"}</button></div>)}</div>
    </section>
  );
}

function AuditActivity({ session }) {
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    apiRequest("/admin/audit-logs?limit=12", {}, session.access_token)
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

function AppShell({ session, onLogout }) {
  const [tickets, setTickets] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [query, setQuery] = useState("");
  const [newMessage, setNewMessage] = useState("");
  const [comment, setComment] = useState("");
  const [comments, setComments] = useState([]);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);

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

  useEffect(() => { loadTickets(); }, [statusFilter]);

  useEffect(() => {
    if (!selectedTicket) { setComments([]); return; }
    apiRequest(`/tickets/${selectedTicket.id}/comments`, {}, session.access_token).then(setComments).catch((error) => setNotice(error.message));
  }, [selectedId]);

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

  const visibleTickets = tickets.filter((ticket) => `${ticket.message} ${ticket.intent} ${ticket.customer_id}`.toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand"><div className="brand-mark small"><span>R</span></div><strong>relay</strong></div>
        <div className="workspace-label">Workspace</div>
        <nav><a className="nav-item active"><Inbox size={17} /> Inbox <span>{tickets.length}</span></a><a className="nav-item"><MessageSquare size={17} /> Conversations</a></nav>
        <div className="sidebar-bottom"><div className="user-chip"><div className="avatar"><UserRound size={16} /></div><div><strong>{session.user.email.split("@")[0]}</strong><small>{session.user.role}</small></div></div><button className="logout-button" onClick={onLogout} title="Sign out"><LogOut size={17} /></button></div>
      </aside>
      <main className="workspace">
        <header className="topbar"><div><p className="eyebrow">{isStaff ? "Agent workspace" : "Customer portal"}</p><h1>{isStaff ? "Support queue" : "Your conversations"}</h1></div><div className="topbar-actions"><span className="live-status"><span /> System healthy</span><button className="icon-button" onClick={loadTickets} title="Refresh tickets"><RefreshCw size={17} /></button></div></header>
        <section className="stats-row"><div><span>Open tickets</span><strong>{tickets.filter((ticket) => ticket.status === "open").length}</strong></div><div><span>Needs attention</span><strong>{tickets.filter((ticket) => ticket.requires_human_review).length}</strong></div><div><span>In progress</span><strong>{tickets.filter((ticket) => ticket.status === "in_progress").length}</strong></div></section>
        {notice && <div className="notice"><Check size={15} /> {notice}<button onClick={() => setNotice("")}>Dismiss</button></div>}
        {session.user.role === "admin" && <><AdminTeamPanel session={session} /><AuditActivity session={session} /></>}
        <section className="desk-grid">
          <div className="ticket-column">
            <div className="toolbar"><div className="search-box"><Search size={16} /><input placeholder="Search tickets" value={query} onChange={(event) => setQuery(event.target.value)} /></div><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="">All statuses</option><option value="open">Open</option><option value="in_progress">In progress</option><option value="pending">Pending</option><option value="resolved">Resolved</option></select></div>
            <div className="ticket-list">{loading ? <div className="empty-state">Loading queue...</div> : visibleTickets.length ? visibleTickets.map((ticket) => <button className={`ticket-row ${ticket.id === selectedId ? "selected" : ""}`} key={ticket.id} onClick={() => setSelectedId(ticket.id)}><div className="ticket-row-top"><span className={`status-dot ${ticket.status}`} /> <strong>{ticket.intent.replace("_", " ")}</strong><span className={`priority ${ticket.priority}`}>{ticket.priority}</span></div><p>{ticket.message}</p><small>{ticket.customer_id} · {new Date(ticket.created_at).toLocaleDateString()}</small></button>) : <div className="empty-state"><Inbox size={28} /><strong>No tickets here</strong><span>New conversations will appear in this queue.</span></div>}</div>
            {!isStaff && <form className="new-ticket" onSubmit={createTicket}><label>Open a new conversation<textarea value={newMessage} onChange={(event) => setNewMessage(event.target.value)} placeholder="Tell us what happened..." rows="3" /></label><button className="secondary-button"><Plus size={16} /> Submit ticket</button></form>}
          </div>
          <div className="detail-column">{selectedTicket ? <><div className="detail-header"><div><span className="detail-kicker">Ticket {selectedTicket.id.slice(0, 8)}</span><h2>{selectedTicket.message}</h2><p className="muted">Created {new Date(selectedTicket.created_at).toLocaleString()} · {selectedTicket.channel}</p></div><span className={`priority large ${selectedTicket.priority}`}>{selectedTicket.priority}</span></div><div className="detail-meta"><div><span>Intent</span><strong>{selectedTicket.intent.replace("_", " ")}</strong></div><div><span>Customer</span><strong>{selectedTicket.customer_id}</strong></div><div><span>Review</span><strong>{selectedTicket.requires_human_review ? "Human review" : "Automated"}</strong></div></div>{isStaff && <div className="status-actions"><span>Move ticket</span>{["open", "in_progress", "pending", "resolved", "closed"].map((status) => <button className={selectedTicket.status === status ? "active" : ""} key={status} onClick={() => updateTicket(status)}>{status.replace("_", " ")}</button>)}</div>}<div className="conversation"><div className="conversation-heading"><h3>Conversation</h3><span>{comments.length} messages</span></div>{comments.length ? comments.map((item) => <article className={`message ${item.is_internal ? "internal" : ""}`} key={item.id}><div className="message-avatar"><UserRound size={15} /></div><div><div className="message-meta"><strong>{item.author_id === session.user.id ? "You" : item.author_id}</strong>{item.is_internal && <span>Internal note</span>}<time>{new Date(item.created_at).toLocaleString()}</time></div><p>{item.body}</p></div></article>) : <div className="empty-conversation">No messages yet. Add the first reply.</div>}<form className="comment-form" onSubmit={addComment}><textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder={isStaff ? "Write an internal note..." : "Write a reply..."} rows="3" /><button className="primary-button">Send <ArrowRight size={16} /></button></form></div></> : <div className="empty-detail"><MessageSquare size={34} /><h2>Select a ticket</h2><p>Choose a conversation from the queue to see its history.</p></div>}</div>
        </section>
      </main>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState(() => JSON.parse(localStorage.getItem("relay-session") || "null"));
  function handleAuthenticated(nextSession) { localStorage.setItem("relay-session", JSON.stringify(nextSession)); setSession(nextSession); }
  function logout() { localStorage.removeItem("relay-session"); setSession(null); }
  return session ? <AppShell session={session} onLogout={logout} /> : <AuthScreen onAuthenticated={handleAuthenticated} />;
}

createRoot(document.getElementById("root")).render(<App />);