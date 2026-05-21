// Topbar — logo, program name, live badge, cycle info, escalation count
// Matches atis_dashboard_v3.html .topbar styles exactly

const S = {
  topbar: {
    background: '#ffffff',
    borderBottom: '2px solid #0f172a',
    padding: '14px 28px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    flexShrink: 0,
  },
  topLeft: { display: 'flex', alignItems: 'center', gap: '16px' },
  logo: {
    fontSize: '18px', fontWeight: 700, fontStyle: 'italic',
    color: '#0f172a', letterSpacing: '-0.02em',
    fontFamily: 'Georgia, serif',
  },
  progPill: {
    fontSize: '12px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif',
    background: '#f5f5f4', border: '0.5px solid #d6d3d1',
    padding: '4px 12px', borderRadius: '4px',
  },
  liveBadge: {
    fontSize: '10px', color: '#16a34a',
    background: '#f0fdf4', border: '0.5px solid #bbf7d0',
    padding: '3px 10px', borderRadius: '3px',
    fontFamily: 'system-ui, sans-serif',
    letterSpacing: '0.07em', textTransform: 'uppercase',
    fontWeight: 600,
  },
  disconnectedBadge: {
    fontSize: '10px', color: '#dc2626',
    background: '#fef2f2', border: '0.5px solid #fecaca',
    padding: '3px 10px', borderRadius: '3px',
    fontFamily: 'system-ui, sans-serif',
    letterSpacing: '0.07em', textTransform: 'uppercase',
    fontWeight: 600,
  },
  connectingBadge: {
    fontSize: '10px', color: '#d97706',
    background: '#fffbeb', border: '0.5px solid #fde68a',
    padding: '3px 10px', borderRadius: '3px',
    fontFamily: 'system-ui, sans-serif',
    letterSpacing: '0.07em', textTransform: 'uppercase',
    fontWeight: 600,
  },
  topRight: { display: 'flex', alignItems: 'center', gap: '24px' },
  cycleInfo: {
    fontSize: '11px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif',
  },
  escFlag: {
    fontSize: '12px', fontWeight: 700, color: '#dc2626',
    fontFamily: 'system-ui, sans-serif',
    display: 'flex', alignItems: 'center', gap: '6px',
  },
  escDot: {
    width: '8px', height: '8px', borderRadius: '50%',
    background: '#dc2626',
  },
}

function _timeSince(isoStr) {
  if (!isoStr) return null
  const secs = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000)
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  return `${mins}m ago`
}

export default function Topbar({ program, wsStatus, lastCycle, escalationCount }) {
  const programLabel = program
    ? `${program.name} — ${program.domain.replace(/_/g, ' ')}`
    : 'Loading…'

  const badge =
    wsStatus === 'live'          ? <span style={S.liveBadge}>● Live</span>
    : wsStatus === 'connecting'  ? <span style={S.connectingBadge}>◌ Connecting</span>
    :                              <span style={S.disconnectedBadge}>✕ Disconnected</span>

  return (
    <header style={S.topbar}>
      <div style={S.topLeft}>
        <span style={S.logo}>ATIS</span>
        <span style={S.progPill}>{programLabel}</span>
        {badge}
      </div>
      <div style={S.topRight}>
        {lastCycle && (
          <span style={S.cycleInfo}>
            Cycle <strong style={{ color: '#0f172a', fontWeight: 700 }}>#{lastCycle.cycle_number}</strong>
            {lastCycle.completed_at && ` · ${_timeSince(lastCycle.completed_at)}`}
          </span>
        )}
        {escalationCount > 0 && (
          <span style={S.escFlag}>
            <span style={S.escDot} className="atis-pulse" />
            {escalationCount} escalation{escalationCount !== 1 ? 's' : ''} active
          </span>
        )}
      </div>
    </header>
  )
}
