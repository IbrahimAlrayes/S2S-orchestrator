import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  output: 'standalone',
  // ESLint and TypeScript checks run in the CI validate job.
  // Disabling them here keeps the Docker build fast and avoids
  // false failures from CRLF line endings on Windows dev machines.
  eslint: {
    ignoreDuringBuilds: true,
  },
  typescript: {
    ignoreBuildErrors: false,
  },
};

export default nextConfig;
