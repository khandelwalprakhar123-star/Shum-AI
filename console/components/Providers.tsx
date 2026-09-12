"use client";

import { CopilotKitProvider } from "@copilotkit/react-core/v2";

/**
 * v2, not v1.
 *
 * Every tutorial online still shows `<CopilotKit>` with `useCopilotAction`.
 * That is the v1 API and it is deprecated. v2 is `CopilotKitProvider` plus
 * `useFrontendTool` / `useHumanInTheLoop` / `useAgent`, imported from
 * `@copilotkit/react-core/v2`. Mixing them silently gives you a chat that
 * connects and then never calls any of your tools.
 *
 * `agentId` names the agent registered in the runtime route, so no chat has to
 * repeat it.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <CopilotKitProvider runtimeUrl="/api/copilotkit" agentId="operator">
      {children}
    </CopilotKitProvider>
  );
}
