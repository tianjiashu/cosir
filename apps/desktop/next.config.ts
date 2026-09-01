import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  turbopack: {
    root: __dirname,
  },
  // The desktop dev server is commonly opened as 127.0.0.1 while Next.js
  // resolves its dev resources through localhost (and vice versa).
  allowedDevOrigins: ["127.0.0.1", "localhost"],
};

export default nextConfig;
