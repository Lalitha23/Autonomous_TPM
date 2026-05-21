// SummaryBar — 7 summary metrics always visible below topbar
// Matches atis_dashboard_v3.html .summary-bar styles

const SEV_COLOR = {
  ESCALATE: '#dc2626',
  ALERT:    '#ea580c',
  WATCH:    '#d97706',
  HEALTHY:  '#16a34a',
  null:     '#0f172a',
}

const S = {
  bar: {
    background: '#ffffff',
    borderBottom: '0.5px solid #d6d3d1',
    padding: '14px 28px',
    display: 'flex',
    alignItems: 'center',
    gap: '32px',
    flexShrink: 0,
    overflowX: 'auto',
  },
  item: { display: 'flex', flexDirection: 'column', gap: '3px', flexShrink: 0 },
  label: {
    fontSize: '9px', fontWeight: 700, letterSpacing: '0.1em',
    textTransform: 'uppercase', color: '#44403c',
    fontFamily: 'system-ui, sans-serif',
  },
  value: {
    fontSize: '20px', fontWeight: 700,
    fontFamily: 'system-ui, sans-serif', lineHeight: 1,
  },
  sub: { fontSize: '10px', color: '#44403c', fontFamily: 'system-ui, sans-serif' },
  divider: { width: '1px', height: '36px', background: '#d6d3d1', flexShrink: 0 },
}

function Item({ label, value, sub, color }) {
  return (
    <div style={S.item}>
      <span style={S.label}>{label}</span>
      <span style={{ ...S.value, color: color || '#0f172a' }}>{value}</span>
      {sub && <span style={S.sub}>{sub}</span>}
    </div>
  )
}

function weeksToLaunch(program) {
  // Try to extract launch_target from context_config
  try {
    const lt = program?.context_config?.launch_target
    if (!lt) return '—'
    const diff = new Date(lt) - new Date()
    const weeks = Math.ceil(diff / (1000 * 60 * 60 * 24 * 7))
    return weeks > 0 ? `${weeks}w` : '0w'
  } catch {
    return '—'
  }
}

export default function SummaryBar({
  sprints, worstBadge, activeRisks, critHighCount,
  flaggedCount, totalTickets, program
}) {
  const launchWeeks = weeksToLaunch(program)
  const worstColor = SEV_COLOR[worstBadge] || '#0f172a'
  const worstLabel = worstBadge
    ? worstBadge.charAt(0) + worstBadge.slice(1).toLowerCase()
    : '—'

  return (
    <div style={S.bar}>
      <Item
        label="Program status"
        value={worstLabel}
        sub="Worst active severity"
        color={worstColor}
      />
      <div style={S.divider} />
      <Item
        label="Active risks"
        value={activeRisks}
        sub={`${critHighCount} critical / high`}
        color="#0f172a"
      />
      <div style={S.divider} />
      {sprints.map((s, i) => (
        <div key={s.sprint_id} style={{ display: 'flex', alignItems: 'stretch', gap: '32px' }}>
          <Item
            label={s.name.split(' — ')[0]}
            value={s.pct_complete != null ? `${Math.round(s.pct_complete)}%` : '—'}
            sub={s.health_badge
              ? s.health_badge.charAt(0) + s.health_badge.slice(1).toLowerCase()
              : 'No data'}
            color={SEV_COLOR[s.health_badge] || '#0f172a'}
          />
          {i < sprints.length - 1 && <div style={S.divider} />}
        </div>
      ))}
      <div style={S.divider} />
      <Item
        label="Tickets flagged"
        value={flaggedCount}
        sub={`of ${totalTickets} total`}
        color="#0f172a"
      />
      <div style={S.divider} />
      <Item
        label="Launch target"
        value={launchWeeks}
        sub="Q3 commitment"
        color="#0f172a"
      />
    </div>
  )
}
