import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** 渲染 Markdown；把 [E12] 变成可点击的证据引用，把【事实】等标签高亮。 */
export default function Markdown({ text, onEvidence }: { text: string; onEvidence?: (label: string) => void }) {
  const prepared = (text || "")
    .replace(/\[(E\d+)\]/g, (_m, l) => `[${l}](#ev-${l})`)
    .replace(/【事实】/g, "`【事实】`")
    .replace(/【推断】/g, "`【推断】`")
    .replace(/【不确定[^】]*】/g, (m) => `\`${m}\``);
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            if (href && href.startsWith("#ev-")) {
              const label = href.slice(4);
              return (
                <a className="evref" href={href} onClick={(e) => { e.preventDefault(); onEvidence?.(label); }}>
                  {children}
                </a>
              );
            }
            return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
          },
          code: ({ children }) => {
            const s = String(children);
            if (s.startsWith("【事实】")) return <span className="tag fact">事实</span>;
            if (s.startsWith("【推断】")) return <span className="tag infer">推断</span>;
            if (s.startsWith("【不确定")) return <span className="tag uncertain">{s.replace(/[【】]/g, "")}</span>;
            return <code>{children}</code>;
          },
        }}
      >
        {prepared}
      </ReactMarkdown>
    </div>
  );
}
