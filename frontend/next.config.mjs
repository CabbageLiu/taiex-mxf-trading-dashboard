/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const api = process.env.API_BASE || "http://127.0.0.1:8000";
    return [
      { source: "/api/:path*", destination: `${api}/:path*` },
      { source: "/ws/:path*", destination: `${api}/ws/:path*` },
    ];
  },
};
export default nextConfig;
