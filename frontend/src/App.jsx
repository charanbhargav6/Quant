import { useState, useEffect } from 'react';
import { supabase } from './supabase';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, ResponsiveContainer,
  PieChart, Pie, Cell
} from 'recharts';
import {
  LayoutDashboard, CreditCard, Award, BarChart2, List,
  Newspaper, Calendar, Monitor, Calculator, TrendingUp,
  MoreVertical, Share2, Key
} from 'lucide-react';
import './index.css';

const COLORS = ['#2e5cff', '#00c853', '#ff4d4f', '#00e5ff', '#ffab00', '#d500f9'];

function App() {
  const [stats, setStats] = useState({
    equity: 10000,
    starting_equity: 10000,
    profit_factor: 0,
    win_rate: 0,
    total_trades: 0,
    wins: 0,
    losses: 0,
    max_drawdown_pct: 0
  });
  
  const [curve, setCurve] = useState([]);
  const [trades, setTrades] = useState([]);
  const [openPositions, setOpenPositions] = useState([]);
  const [systemStatus, setSystemStatus] = useState(null);

  useEffect(() => {
    fetchData();

    const statsSub = supabase.channel('stats_changes')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'crave_account_stats' }, fetchData)
      .subscribe();
      
    const tradesSub = supabase.channel('trades_changes')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'crave_trades' }, fetchData)
      .subscribe();
      
    const openPosSub = supabase.channel('open_pos_changes')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'crave_open_positions' }, fetchData)
      .subscribe();

    const statusSub = supabase.channel('status_changes')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'crave_system_status' }, fetchData)
      .subscribe();

    return () => {
      supabase.removeChannel(statsSub);
      supabase.removeChannel(tradesSub);
      supabase.removeChannel(openPosSub);
      supabase.removeChannel(statusSub);
    };
  }, []);

  async function fetchData() {
    // Fetch stats
    const { data: statsData } = await supabase.from('crave_account_stats').select('*').limit(1).single();
    if (statsData) setStats(statsData);

    // Fetch equity curve
    const { data: curveData } = await supabase.from('crave_equity_curve').select('*').order('id', { ascending: true });
    if (curveData) {
      const formattedCurve = curveData.map((d, i) => ({ name: i, equity: d.equity }));
      if (formattedCurve.length === 0) formattedCurve.push({ name: 0, equity: statsData?.equity || 10000 });
      setCurve(formattedCurve);
    }

    // Fetch trades (closed journal — used for table, Most Traded and avg R)
    const { data: tradesData } = await supabase.from('crave_trades').select('*').order('close_time', { ascending: false }).limit(200);
    if (tradesData) setTrades(tradesData);

    // Fetch open positions
    const { data: posData } = await supabase.from('crave_open_positions').select('*');
    if (posData) setOpenPositions(posData);

    // Fetch live system status (node / mode / heartbeat / uptime)
    const { data: statusData } = await supabase.from('crave_system_status').select('*').limit(1).single();
    if (statusData) setSystemStatus(statusData);
  }

  const profitPct = (((stats.equity - stats.starting_equity) / stats.starting_equity) * 100).toFixed(2);
  const netProfit = stats.equity - stats.starting_equity;
  const dailyPnlPct = Number(stats.daily_pnl_pct ?? 0);

  // Average win / loss in R-multiples (computed from real closed trades; the
  // engine does not track per-trade dollar P&L, so we report R like the journal).
  const winR  = trades.filter(t => Number(t.r_multiple) > 0).map(t => Number(t.r_multiple));
  const lossR = trades.filter(t => Number(t.r_multiple) < 0).map(t => Number(t.r_multiple));
  const avgWinR  = winR.length  ? (winR.reduce((a, b) => a + b, 0) / winR.length) : null;
  const avgLossR = lossR.length ? Math.abs(lossR.reduce((a, b) => a + b, 0) / lossR.length) : null;

  // Calculate Most Traded
  const symbolCounts = trades.reduce((acc, t) => {
    acc[t.symbol] = (acc[t.symbol] || 0) + 1;
    return acc;
  }, {});
  
  const pieData = Object.keys(symbolCounts).map((key, i) => ({
    name: key,
    value: symbolCounts[key],
    color: COLORS[i % COLORS.length]
  }));

  // Removed fake data fallback

  return (
    <div className="dashboard-container">
      {/* SIDEBAR */}
      <div className="sidebar">
        <div className="logo-section">
          <TrendingUp className="logo-icon" /> CRAVE
        </div>
        
        <div className="nav-section">
          <div className="nav-label">MENU</div>
          <a className="nav-item active"><LayoutDashboard /> Accounts Overview</a>
          <a className="nav-item"><CreditCard /> Payouts</a>
          <a className="nav-item"><Award /> Certificates</a>
          <a className="nav-item"><BarChart2 /> Leaderboard</a>
          <a className="nav-item"><List /> Order List</a>
          
          <div className="nav-label">APPS</div>
          <a className="nav-item"><Newspaper /> News Feeds</a>
          <a className="nav-item"><Calendar /> Economic Calendar</a>
          <a className="nav-item"><Monitor /> WebTrader Platform</a>
          <a className="nav-item"><Calculator /> Margin Calculator</a>
        </div>

        <div className="user-profile">
          <div className="avatar" style={{display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--primary-color)', color: '#fff'}}>
            <TrendingUp size={18} />
          </div>
          <div className="user-info">
            <div className="user-name">CRAVE Engine</div>
            <div className="user-email">
              {systemStatus ? `${systemStatus.active_node} · ${systemStatus.trading_mode}` : 'connecting…'}
            </div>
          </div>
        </div>
      </div>

      {/* MAIN CONTENT */}
      <div className="main-content">
        <div className="header">
          <h1>Accounts Overview{systemStatus ? ` — ${systemStatus.trading_mode} mode` : ''}</h1>
          <div className="header-actions">
            <button className="btn btn-primary"><CreditCard size={16}/> Request Payout</button>
            <button className="btn btn-secondary"><Share2 size={16}/> Share Matrices</button>
            <button className="btn btn-secondary"><Key size={16}/></button>
          </div>
        </div>

        <div className="dashboard-grid">
          {/* LEFT COLUMN */}
          <div className="flex-col">
            
            {/* Chart Card */}
            <div className="card">
              <div className="card-header">
                <div>
                  <div style={{color: 'var(--text-muted)', fontSize: '14px', marginBottom: '4px'}}>Total Balance</div>
                  <div style={{fontSize: '24px', fontWeight: '700', display: 'flex', alignItems: 'center', gap: '12px'}}>
                    ${Number(stats.equity).toLocaleString('en-US', {minimumFractionDigits: 2})}
                    <span style={{fontSize: '14px', color: profitPct >= 0 ? 'var(--primary-color)' : 'var(--danger)', fontWeight: '600'}}>
                      Profit: {profitPct}%
                    </span>
                  </div>
                </div>
              </div>
              <div style={{height: '240px', width: '100%'}}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={curve}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#eef0f6" />
                    <XAxis dataKey="name" hide />
                    <YAxis domain={['auto', 'auto']} axisLine={false} tickLine={false} tick={{fill: '#8b92a5', fontSize: 12}} />
                    <RechartsTooltip contentStyle={{borderRadius: '8px', border: 'none', boxShadow: 'var(--shadow-md)'}} />
                    <Line type="monotone" dataKey="equity" stroke="#2e5cff" strokeWidth={3} dot={false} activeDot={{r: 6}} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>

            {/* Stats Grid */}
            <div className="stats-grid">
              <div className="stat-box">
                <div className="stat-label"><TrendingUp size={16} color="var(--primary-color)"/> Avg Win (R)</div>
                <div className="stat-value">{avgWinR !== null ? `+${avgWinR.toFixed(2)}R` : '—'}</div>
              </div>
              <div className="stat-box">
                <div className="stat-label"><TrendingUp size={16} color="var(--danger)"/> Avg Loss (R)</div>
                <div className="stat-value">{avgLossR !== null ? `-${avgLossR.toFixed(2)}R` : '—'}</div>
              </div>
              <div className="stat-box">
                <div className="stat-label"><BarChart2 size={16} color="var(--primary-color)"/> Profit Factor</div>
                <div className="stat-value">{stats.profit_factor}</div>
              </div>
              <div className="stat-box">
                <div className="stat-label"><Award size={16} color="var(--primary-color)"/> Total Trades</div>
                <div className="stat-value">{stats.total_trades}</div>
              </div>
              <div className="stat-box">
                <div className="stat-label"><Award size={16} color="var(--success)"/> Win Ratio</div>
                <div className="stat-value">{(stats.win_rate * 100).toFixed(1)}%</div>
              </div>
              <div className="stat-box">
                <div className="stat-label"><Award size={16} color="var(--danger)"/> Max Drawdown</div>
                <div className="stat-value">{stats.max_drawdown_pct}%</div>
              </div>
            </div>

            {/* Order History Table */}
            <div className="card">
              <div className="card-header" style={{marginBottom: 0}}>
                <div className="card-title">Order History</div>
                <div className="header-actions">
                  <button className="btn btn-primary" style={{padding: '6px 12px', fontSize: '12px'}}>Open Trades ({openPositions.length})</button>
                  <button className="btn btn-secondary" style={{padding: '6px 12px', fontSize: '12px'}}>Closed Trades</button>
                </div>
              </div>
              
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Type</th>
                    <th>Open Price</th>
                    <th>SL</th>
                    <th>Profit (R)</th>
                  </tr>
                </thead>
                <tbody>
                  {openPositions.map((pos) => (
                    <tr key={pos.trade_id}>
                      <td style={{fontWeight: 600}}>{pos.symbol}</td>
                      <td><span className={`badge ${pos.direction === 'buy' || pos.direction === 'long' ? 'buy' : 'sell'}`}>{pos.direction.toUpperCase()}</span></td>
                      <td>{pos.entry_price}</td>
                      <td>{pos.current_sl}</td>
                      <td className={pos.unrealised_r >= 0 ? 'success' : 'danger'} style={{fontWeight: 600}}>
                        {pos.unrealised_r >= 0 ? '+' : ''}{pos.unrealised_r} R
                      </td>
                    </tr>
                  ))}
                  {trades.slice(0, 3).map((t) => (
                    <tr key={t.trade_id}>
                      <td style={{fontWeight: 600}}>{t.symbol}</td>
                      <td><span className={`badge ${t.direction === 'buy' || t.direction === 'long' ? 'buy' : 'sell'}`}>{t.direction.toUpperCase()}</span></td>
                      <td>{t.entry_price}</td>
                      <td>{t.stop_loss}</td>
                      <td className={t.r_multiple >= 0 ? 'success' : 'danger'} style={{fontWeight: 600}}>
                        {t.r_multiple >= 0 ? '+' : ''}{t.r_multiple} R
                      </td>
                    </tr>
                  ))}
                  {trades.length === 0 && openPositions.length === 0 && (
                     <tr><td colSpan="5" style={{textAlign: 'center', color: 'var(--text-muted)'}}>No trades recorded yet</td></tr>
                  )}
                </tbody>
              </table>
            </div>

          </div>

          {/* RIGHT COLUMN */}
          <div className="flex-col">
            
            <div className="flex-row" style={{gap: '16px'}}>
              <div className="card" style={{flex: 1}}>
                <div className="target-widget">
                  <div className="target-header">
                    <div className="target-icon icon-blue"><Award size={20}/></div>
                    <div>
                      <div style={{fontWeight: 600, fontSize: '14px'}}>Net Profit</div>
                      <div style={{fontSize: '12px', color: 'var(--text-muted)'}}>Since start</div>
                    </div>
                  </div>
                  <div className="target-amount">{netProfit >= 0 ? '+' : '-'}${Math.abs(netProfit).toLocaleString('en-US', {minimumFractionDigits: 2})}</div>
                  <div className="target-subtext" style={{color: netProfit >= 0 ? 'var(--primary-color)' : 'var(--danger)'}}>Total Return {profitPct}%</div>
                </div>
              </div>

              <div className="card" style={{flex: 1}}>
                <div className="target-widget">
                  <div className="target-header">
                    <div className="target-icon icon-red"><BarChart2 size={20}/></div>
                    <div>
                      <div style={{fontWeight: 600, fontSize: '14px'}}>Daily P&amp;L</div>
                      <div style={{fontSize: '12px', color: 'var(--text-muted)'}}>Today</div>
                    </div>
                  </div>
                  <div className="target-amount">{dailyPnlPct >= 0 ? '+' : ''}{dailyPnlPct.toFixed(2)}%</div>
                  <div className="target-subtext" style={{color: stats.circuit_breaker ? 'var(--danger)' : 'var(--text-muted)'}}>
                    {stats.circuit_breaker ? 'Circuit breaker ACTIVE' : `Streak: ${stats.streak_state || 'neutral'}`}
                  </div>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-header"><div className="card-title">Most Traded</div></div>
              <div className="donut-container">
                <div className="legend-list">
                  {pieData.map(entry => (
                    <div className="legend-item" key={entry.name}>
                      <div className="dot" style={{backgroundColor: entry.color}}></div>
                      {entry.name}
                    </div>
                  ))}
                </div>
                <div style={{width: '120px', height: '120px'}}>
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={pieData} innerRadius={40} outerRadius={60} paddingAngle={5} dataKey="value" stroke="none">
                        {pieData.map((entry, index) => (
                          <Cell key={`cell-${index}`} fill={entry.color} />
                        ))}
                      </Pie>
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              </div>
            </div>

            <div className="card" style={{flex: 1}}>
              <div className="card-header"><div className="card-title">System Status</div></div>
              <div style={{display: 'flex', flexDirection: 'column', gap: '16px'}}>
                 {systemStatus ? (
                   <div style={{fontSize: '13px', borderBottom: '1px solid var(--border-color)', paddingBottom: '12px'}}>
                      <div style={{color: 'var(--text-muted)', fontSize: '11px'}}>
                        Last heartbeat {new Date(systemStatus.last_heartbeat).toLocaleString()}
                      </div>
                      <div style={{fontWeight: 600, marginTop: '4px'}}>
                        {systemStatus.bot_running ? '🟢 Engine Online' : '🔴 Engine Offline'} — node “{systemStatus.active_node}”
                      </div>
                      <div style={{color: 'var(--text-muted)', marginTop: '2px'}}>
                        {systemStatus.trading_mode} mode · {systemStatus.open_positions} open · uptime {Number(systemStatus.uptime_h).toFixed(1)}h · py {systemStatus.python_version}
                      </div>
                   </div>
                 ) : (
                   <div style={{fontSize: '13px', color: 'var(--text-muted)'}}>Connecting to engine…</div>
                 )}
              </div>
            </div>

          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
