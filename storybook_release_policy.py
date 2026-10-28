"""Explicit exact-version Storybook release cohorts.

A namespace is not a release train. Unknown @storybook addons are independently
versioned until this table explicitly declares them; their peer requirements
still participate in ordinary dependency solving. In particular the webpack5
compiler addons use independent majors and must not be pinned to Storybook's
major. This versioned table is deliberately conservative.
"""
STORYBOOK_RELEASE_POLICY_VERSION = 1
STORYBOOK_EXACT_RELEASE_PACKAGES = frozenset({
    "storybook",
    "@storybook/addon-a11y", "@storybook/addon-actions",
    "@storybook/addon-backgrounds", "@storybook/addon-controls",
    "@storybook/addon-docs", "@storybook/addon-essentials",
    "@storybook/addon-highlight", "@storybook/addon-interactions",
    "@storybook/addon-links", "@storybook/addon-measure",
    "@storybook/addon-outline", "@storybook/addon-toolbars",
    "@storybook/addon-viewport", "@storybook/addon-vitest",
    "@storybook/angular", "@storybook/blocks",
    "@storybook/builder-vite", "@storybook/builder-webpack5",
    "@storybook/components", "@storybook/core", "@storybook/core-common",
    "@storybook/core-server", "@storybook/html", "@storybook/html-vite",
    "@storybook/html-webpack5", "@storybook/manager-api",
    "@storybook/nextjs", "@storybook/nextjs-vite", "@storybook/preact",
    "@storybook/preact-vite", "@storybook/preview-api",
    "@storybook/react", "@storybook/react-vite", "@storybook/react-webpack5",
    "@storybook/server", "@storybook/server-webpack5",
    "@storybook/svelte", "@storybook/svelte-vite", "@storybook/sveltekit",
    "@storybook/test", "@storybook/theming", "@storybook/types",
    "@storybook/vue3", "@storybook/vue3-vite", "@storybook/vue3-webpack5",
    "@storybook/web-components", "@storybook/web-components-vite",
    "@storybook/web-components-webpack5",
})


def is_storybook_release_package(name: str) -> bool:
    return name in STORYBOOK_EXACT_RELEASE_PACKAGES
