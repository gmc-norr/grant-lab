import styles from "./Pagination.module.css";

type Props = {
  page: number;
  hasPrev: boolean;
  hasNext: boolean;
  onPage: (page: number) => void;
};

export function Pagination({ page, hasPrev, hasNext, onPage }: Props) {
  return (
    <nav className={styles.pagination} aria-label="Pagination">
      <button type="button" disabled={!hasPrev} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <span>Page {page}</span>
      <button type="button" disabled={!hasNext} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </nav>
  );
}
