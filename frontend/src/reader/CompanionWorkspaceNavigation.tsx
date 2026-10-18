import { Icon, type IconName } from "../ui/Icon";

export type CompanionView = "recap" | "memory" | "ask" | "reflect";

const views: Array<{ id: CompanionView; label: string; icon: IconName }> = [
  { id: "recap", label: "Recap", icon: "book" },
  { id: "memory", label: "People & chapters", icon: "library" },
  { id: "ask", label: "Ask the book", icon: "sparkle" },
  { id: "reflect", label: "Reflect", icon: "bookmark" },
];

export function CompanionWorkspaceNavigation({ active, onNavigate, unit = "chapter", canReflect }: {
  active: CompanionView;
  onNavigate: (view: CompanionView) => void;
  unit?: string;
  canReflect: boolean;
}) {
  return (
    <nav className="companion-workspace-nav" aria-label="Companion workspace">
      {views.map((view) => (
        <button
          key={view.id}
          type="button"
          aria-current={active === view.id ? "page" : undefined}
          disabled={view.id === "reflect" && !canReflect}
          title={view.id === "reflect" && !canReflect ? `Available when your completed ${unit} is prepared` : undefined}
          onClick={() => onNavigate(view.id)}
        >
          <Icon name={view.icon} size={16} />
          <span>{view.id === "memory" && unit !== "chapter" ? "People & sections" : view.label}</span>
        </button>
      ))}
    </nav>
  );
}
