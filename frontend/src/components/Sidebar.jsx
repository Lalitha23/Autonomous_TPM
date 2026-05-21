// Sidebar — 4 nav items with badges
// Matches atis_dashboard_v3.html .sidebar styles

const S = {
  sidebar: {
    width: '200px', flexShrink: 0, background: '#ffffff',
    borderRight: '0.5px solid #d6d3d1', padding: '24px 0',
    display: 'flex', flexDirection: 'column', gap: '4px',
  },
  section: {
    fontSize: '9px', fontWeight: 700, letterSpacing: '0.12em',
    textTransform: 'uppercase', color: '#57534e',
    fontFamily: 'system-ui, sans-serif',
    padding: '0 20px', marginBottom: '6px', marginTop: '16px',
  },
  navItem: (active) => ({
    display: 'flex', alignItems: 'center', gap: '10px',
    padding: '10px 20px', cursor: 'pointer',
    fontFamily: 'system-ui, sans-serif', fontSize: '13px',
    color: active ? '#0f172a' : '#44403c',
    borderLeft: active ? '2px solid #0f172a' : '2px solid transparent',
    background: active ? '#fafaf9' : 'transparent',
    fontWeight: active ? 600 : 400,
    userSelect: 'none', transition: 'all 0.12s',
  }),
  icon: (active) => ({
    width: '16px', height: '16px', flexShrink: 0,
    opacity: active ? 1 : 0.7,
  }),
  badge: (variant) => ({
    marginLeft: 'auto', fontSize: '9px', fontWeight: 700,
    padding: '2px 6px', borderRadius: '10px',
    fontFamily: 'system-ui, sans-serif',
    background: variant === 'red' ? '#fef2f2' : '#e7e5e4',
    color: variant === 'red' ? '#dc2626' : '#44403c',
  }),
}

const PANELS = [
  {
    id: 'health',
    label: 'Program Health',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="1" y="3" width="14" height="2.5" rx="1"/>
        <rect x="1" y="7.5" width="10" height="2.5" rx="1"/>
        <rect x="1" y="12" width="12" height="2.5" rx="1"/>
      </svg>
    ),
  },
  {
    id: 'backlog',
    label: 'Backlog View',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="1" y="1" width="14" height="14" rx="2"/>
        <line x1="4" y1="5" x2="12" y2="5"/>
        <line x1="4" y1="8" x2="12" y2="8"/>
        <line x1="4" y1="11" x2="9" y2="11"/>
      </svg>
    ),
  },
  {
    id: 'activity',
    label: 'Agent Activity',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <polyline points="1,11 4,6 7,9 10,3 13,7 15,5"/>
      </svg>
    ),
  },
  {
    id: 'outputs',
    label: 'Executive Outputs',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="2" y="1" width="12" height="14" rx="1.5"/>
        <line x1="5" y1="5" x2="11" y2="5"/>
        <line x1="5" y1="8" x2="11" y2="8"/>
        <line x1="5" y1="11" x2="8" y2="11"/>
      </svg>
    ),
  },
]

export default function Sidebar({
  activePanel, onSelect, escalationCount, flaggedCount, decisionCount
}) {
  const badges = {
    health:   escalationCount > 0 ? { label: '!', variant: 'red' } : null,
    backlog:  flaggedCount > 0    ? { label: String(flaggedCount), variant: 'red' } : null,
    activity: decisionCount > 0   ? { label: String(Math.min(decisionCount, 99)), variant: 'gray' } : null,
    outputs:  null,
  }

  return (
    <nav style={S.sidebar}>
      <div style={{ ...S.section, marginTop: 0 }}>Panels</div>
      {PANELS.map(({ id, label, icon }) => {
        const active = activePanel === id
        const badge = badges[id]
        return (
          <div
            key={id}
            style={S.navItem(active)}
            onClick={() => onSelect(id)}
            onMouseEnter={e => {
              if (!active) {
                e.currentTarget.style.background = '#fafaf9'
                e.currentTarget.style.color = '#0f172a'
              }
            }}
            onMouseLeave={e => {
              if (!active) {
                e.currentTarget.style.background = 'transparent'
                e.currentTarget.style.color = '#44403c'
              }
            }}
          >
            <span style={S.icon(active)}>{icon}</span>
            {label}
            {badge && (
              <span style={S.badge(badge.variant)}>{badge.label}</span>
            )}
          </div>
        )
      })}
    </nav>
  )
}
