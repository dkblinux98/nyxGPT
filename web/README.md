# nyxGPT Web UI

Next.js-based web interface for nyxGPT with optimized performance and modern features.

This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Image Optimization

The web UI includes comprehensive image optimization features powered by Next.js Image component:

### Features

- **Automatic format conversion** - Images are automatically converted to modern formats (WebP, AVIF) for optimal file sizes
- **Lazy loading** - Images load only when they enter the viewport, improving initial page load performance
- **Responsive sizing** - Images automatically scale to appropriate sizes for different screen sizes and device pixel densities
- **Blur placeholders** - Low-quality image placeholders display while full images load, preventing layout shift
- **Configurable quality** - Adjustable image quality settings for balancing size vs. visual fidelity

### Configuration

Image optimization is configured in `next.config.ts`:

```typescript
images: {
  formats: ['image/webp', 'image/avif'],
  deviceSizes: [640, 750, 828, 1080, 1200, 1920, 2048, 3840],
  imageSizes: [16, 32, 48, 64, 96, 128, 256, 384],
  minimumCacheTTL: 60,
}
```

### Using the OptimizedImage Component

The `OptimizedImage` component provides a convenient wrapper around Next.js Image with sensible defaults:

```tsx
import { OptimizedImage } from '@/components/OptimizedImage';

// Basic usage
<OptimizedImage
  src="/my-image.png"
  alt="Description"
  width={800}
  height={600}
/>

// With custom quality
<OptimizedImage
  src="/logo.png"
  alt="Logo"
  width={200}
  height={100}
  quality={95}
/>

// With loading skeleton
<OptimizedImage
  src="/photo.jpg"
  alt="Photo"
  width={1200}
  height={800}
  showSkeleton
  skeletonColor="#e0e0e0"
/>
```

### Component Props

- `enableBlur` (boolean, default: `true`) - Enable blur placeholder while loading
- `customBlurDataURL` (string) - Custom base64 encoded blur placeholder
- `showSkeleton` (boolean, default: `false`) - Show animated loading skeleton
- `skeletonColor` (string, default: `'var(--skeleton-bg, #e0e0e0)'`) - Skeleton background color
- `priority` (boolean, default: `false`) - Disable lazy loading for above-the-fold images
- `quality` (number, default: `85`) - Image quality (1-100)
- All standard Next.js Image props are also supported

### Performance Benefits

Image optimization provides several performance improvements:

1. **Reduced bandwidth** - WebP/AVIF formats are 25-35% smaller than PNG/JPEG
2. **Faster page loads** - Lazy loading defers offscreen images
3. **Better UX** - Blur placeholders prevent layout shift
4. **Automatic responsiveness** - Correct image sizes served for each device

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
