/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The bridge is plain-stdlib Python on :8080 and the console is Next on
  // :3000, so every call from here is cross-origin. bridge/server.py sends the
  // CORS headers and answers the OPTIONS preflight. Without them this fails as
  // a bare "Failed to fetch" with no explanation whatsoever, which is a
  // genuinely horrible thing to debug at 15:50.
  async rewrites() {
    return [];
  },
};
export default nextConfig;
