/**
 * A windowed table.
 *
 * ADR-0002 called for virtualised tables, and the reason is concrete: a real
 * CUCM cluster is tens of thousands of lines and devices, and a DOM node per
 * row makes the entity browser unusable at exactly the scale where an operator
 * needs it. Rows are a fixed height so the offset maths is arithmetic rather
 * than measurement, which is what keeps scrolling smooth.
 */

import { useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

const ROW_HEIGHT = 30;
const OVERSCAN = 8;

export interface Column<T> {
  key: string;
  header: string;
  width: number | string;
  render: (row: T) => ReactNode;
  align?: "right";
}

export function VirtualTable<T>({
  rows,
  columns,
  height = 420,
  rowKey,
  selectedKey,
  onSelect,
  empty,
}: {
  rows: T[];
  columns: Column<T>[];
  height?: number;
  rowKey: (row: T) => string;
  selectedKey?: string | null;
  onSelect?: (row: T) => void;
  empty?: ReactNode;
}) {
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(height);
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => setViewport(node.clientHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const visibleCount = Math.ceil(viewport / ROW_HEIGHT) + OVERSCAN * 2;
  const last = Math.min(rows.length, first + visibleCount);
  const slice = rows.slice(first, last);

  const style = (column: Column<T>) => ({
    width: typeof column.width === "number" ? `${column.width}px` : column.width,
    flex: typeof column.width === "number" ? "none" : "1 1 0",
    textAlign: column.align,
  });

  if (rows.length === 0) {
    return <>{empty ?? <div className="empty">Nothing to show.</div>}</>;
  }

  return (
    <div>
      <div className="vtable-head">
        {columns.map((column) => (
          <div key={column.key} className="cell" style={style(column)}>
            {column.header}
          </div>
        ))}
      </div>
      <div
        className="vtable"
        ref={ref}
        style={{ height }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      >
        <div style={{ height: rows.length * ROW_HEIGHT, position: "relative" }}>
          <div style={{ transform: `translateY(${first * ROW_HEIGHT}px)` }}>
            {slice.map((row) => {
              const key = rowKey(row);
              return (
                <div
                  key={key}
                  className={`vtable-row${key === selectedKey ? " selected" : ""}`}
                  onClick={() => onSelect?.(row)}
                >
                  {columns.map((column) => (
                    <div key={column.key} className="cell" style={style(column)}>
                      {column.render(row)}
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
