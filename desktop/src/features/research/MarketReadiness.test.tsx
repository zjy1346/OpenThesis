import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MarketReadiness, FxDetails } from './MarketReadiness';

describe('market prerequisites', () => {
  it('switches to the same issuer listing explicitly', () => {
    const alternative = { cik: 'a', ticker: '300750.SZ', name: 'CATL', exchange: 'SZSE' };
    const onSwitch = vi.fn();
    render(<MarketReadiness company={{ cik: 'h', ticker: '03750.HK', name: 'CATL', exchange: 'HKEX', research_readiness: {
      recommended: false, consecutive_years: 1, reason: 'five_year_history_not_verified', alternative,
    } }} language="en" onSwitch={onSwitch} />);
    expect(screen.getByText(/five-year disclosure history has not been verified/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /300750/ }));
    expect(onSwitch).toHaveBeenCalledWith(alternative);
  });
  it('displays currency direction, date and source', () => {
    render(<FxDetails language="en" snapshot={{ status: 'VERIFIED', fx_rate: .875, fx_source: 'ecb-reference', fx_as_of: '2026-09-09', quote_currency: 'HKD', valuation_currency: 'CNY' }} />);
    expect(screen.getByText(/1 HKD = 0.875000 CNY/)).toHaveTextContent('2026-09-09 · ecb-reference');
  });
  it('distinguishes FX failure from issuer equity scope failure', () => {
    const { rerender } = render(<FxDetails language="en" snapshot={{ status: 'UNAVAILABLE', error_code: 'FX_UNAVAILABLE' }} />);
    expect(screen.getByText(/dated exchange rate/)).toBeInTheDocument();
    rerender(<FxDetails language="en" snapshot={{ status: 'UNAVAILABLE', error_code: 'MARKET_CAP_SCOPE_INCOMPLETE' }} />);
    expect(screen.getByText(/total issuer equity value/)).toBeInTheDocument();
  });
});
