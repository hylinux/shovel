import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

export default function nextConfig(phase: string): NextConfig {
  const development = phase === PHASE_DEVELOPMENT_SERVER;
  return {
    ...(development
      ? {
          logging: {
            incomingRequests: {
              // FastAPI retains failures and slow requests; avoid duplicate proxy polling logs.
              ignore: [/^\/api\/v1\/(?:status|resources|jobs|operations(?:\/[^/?]+)?|resources\/[^/?]+\/profiles|jobs\/[^/?]+\/events)\/?(?:\?|$)/],
            },
            browserToTerminal: true,
          },
          async rewrites() {
            const host = process.env.SHOVEL_BACKEND_HOST || "127.0.0.1";
            const port = process.env.SHOVEL_BACKEND_PORT || "9999";
            return [{ source: "/api/:path*", destination: `http://${host}:${port}/api/:path*` }];
          },
        }
      : { output: "export" }),
    turbopack: {
      rules: {
        "*.css": {
          loaders: ["@tailwindcss/turbopack"],
          as: "*.css",
        },
      },
    },
  };
}
