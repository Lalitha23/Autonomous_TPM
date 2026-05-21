// Panel 1 — Program Health
// Sprint cards with progress bars, health badges
// Matches atis_dashboard_v3.html Panel 1 exactly

const BADGE_STYLE = {
  ESCALATE: { background: '#fef2f2', color: '#dc2626' },
  ALERT:    { background: '#fff7ed', color: '#ea580c' },
  WATCH:    { background: '#fffbeb', color: '#d97706' },
  HEALTHY:  { background: '#f0fdf4', color: '#16a34a' },
}

const BAR_COLOR = {
  ESCALATE: '#dc2626',
  ALERT:    '#ea580c',
  WATCH:    '#d97706',
  HEALTHY:  '#16a34a',
  null:     '#d6d3d1',
}

const PCT_COLOR = {
  ESCALATE: '#dc2626',
  ALERT:    '#ea580c',
  WATCH:    '#d97706',
  HEALTHY:  '#16a34a',
  null:     '#44403c',
}

const S = {
  heading: {
    fontSize: '22px', fontWeight: 700, fontStyle: 'italic',
    color: '#0f172a', marginBottom: '6px',
    fontFamily: 'Georgia, serif',
  },
  sub: {
    fontSize: '13px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif', marginBottom: '28px',
  },
  card: {
    background: '#ffffff', border: '0.5px solid #d6d3d1',
    borderRadius: '8px', padding: '20px 24px',
    marginBottom: '14px', display: 'flex',
    alignItems: 'center', gap: '20px',
  },
  cardLeft: { flex: 1 },
  cardName: {
    fontSize: '15px', fontWeight: 600, color: '#0f172a',
    fontFamily: 'system-ui, sans-serif', marginBottom: '5px',
  },
  cardMeta: {
    fontSize: '12px', color: '#44403c',
    fontFamily: 'system-ui, sans-serif', marginBottom: '12px',
  },
  track: {
    height: '6px', background: '#e7e5e4',
    borderRadius: '3px', overflow: 'hidden',
  },
  fill: { height: '100%', borderRadius: '3px' },
  cardRight: {
    display: 'flex', flexDirection: 'column',
    alignItems: 'flex-end', gap: '8px', flexShrink: 0,
  },
  pct: {
    fontSize: '28px', fontWeight: 700,
    fontFamily: 'system-ui, sans-serif', lineHeight: 1,
  },
  badge: {
    fontSize: '10px', fontWeight: 700,
    letterSpacing: '0.08em', textTransform: 'uppercase',
    padding: '4px 12px', borderRadius: '4px',
    fontFamily: 'system-ui, sans-serif',
  },
  empty: {
    fontSize: '13px', color: '#78716c',
    fontFamily: 'system-ui, sans-serif',
    padding: '40px 0', textAlign: 'center',
  },
}

function sprintMeta(sprint, tickets) {
  const sprintTickets = tickets.filter(t => t.sprint_id === sprint.sprint_id)
  const flagged = sprintTickets.filter(t => t.risk_flag).length
  const teams = [...new Set(sprintTickets.map(t => t.team).filter(Boolean))]
  const parts = []
  if (sprint.ticket_count) parts.push(`${sprint.ticket_count} tickets`)
  if (flagged > 0)         parts.push(`${flagged} flagged`)
  if (teams.length > 0)    parts.push(teams.slice(0, 2).join(' + '))
  return parts.join(' · ') || 'No data'
}

export default function ProgramHealth({ sprints, tickets }) {
  if (!sprints || sprints.length === 0) {
    return (
      <div>
        <div style={S.heading}>Program Health</div>
        <div style={S.sub}>Sprint completion and agent-assessed health status. Updated every 30 seconds.</div>
        <div style={S.empty}>Loading sprint data…</div>
      </div>
    )
  }

  return (
    <div>
      <div style={S.heading}>Program Health</div>
      <div style={S.sub}>Sprint completion and agent-assessed health status. Updated every 30 seconds.</div>
      {sprints.map(s => {
        const badge = s.health_badge || 'HEALTHY'
        const badgeStyle = BADGE_STYLE[badge] || BADGE_STYLE.HEALTHY
        const barColor = BAR_COLOR[badge] || BAR_COLOR.HEALTHY
        const pctColor = PCT_COLOR[badge] || PCT_COLOR.HEALTHY
        const pct = s.pct_complete != null ? Math.round(s.pct_complete) : null

        return (
          <div key={s.sprint_id} style={S.card}>
            <div style={S.cardLeft}>
              <div style={S.cardName}>{s.name}</div>
              <div style={S.cardMeta}>{sprintMeta(s, tickets)}</div>
              <div style={S.track}>
                <div
                  className="atis-bar-fill"
                  style={{
                    ...S.fill,
                    width: `${pct ?? 0}%`,
                    background: barColor,
                  }}
                />
              </div>
            </div>
            <div style={S.cardRight}>
              <span style={{ ...S.pct, color: pct != null ? pctColor : '#44403c' }}>
                {pct != null ? `${pct}%` : '—'}
              </span>
              <span style={{ ...S.badge, ...badgeStyle }}>
                {badge.charAt(0) + badge.slice(1).toLowerCase()}
              </span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
