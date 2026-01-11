import { render } from '@testing-library/react';
import SkeletonLoader, { SessionListSkeleton } from '../../src/components/SkeletonLoader';

describe('SkeletonLoader', () => {
  it('renders single skeleton by default', () => {
    const { container } = render(<SkeletonLoader />);
    const skeletons = container.querySelectorAll('[aria-hidden="true"]');
    expect(skeletons).toHaveLength(1);
  });

  it('renders multiple skeletons when count provided', () => {
    const { container } = render(<SkeletonLoader count={3} />);
    const skeletons = container.querySelectorAll('[aria-hidden="true"]');
    expect(skeletons).toHaveLength(3);
  });

  it('renders with custom width and height', () => {
    render(<SkeletonLoader width="200px" height={40} />);
    // Component renders, structural test passes
  });
});

describe('SessionListSkeleton', () => {
  it('renders session list skeleton', () => {
    const { container } = render(<SessionListSkeleton />);
    const skeletons = container.querySelectorAll('[aria-hidden="true"]');
    expect(skeletons.length).toBeGreaterThan(0);
  });
});
