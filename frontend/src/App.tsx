import React, { Suspense } from "react";
import { Routes, Route, Navigate, Outlet } from "react-router-dom";
import { useAuth } from "./hooks/useAuth";
import Layout from "./components/layout/Layout";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";

const AgentPage = React.lazy(() => import("./pages/AgentPage"));
const ReviewsPage = React.lazy(() => import("./pages/ReviewsPage"));
const WorkflowsPage = React.lazy(() => import("./pages/WorkflowsPage"));
const PayerRulesPage = React.lazy(() => import("./pages/PayerRulesPage"));
const AuditPage = React.lazy(() => import("./pages/AuditPage"));

function PageLoader() {
  return (
    <div className="flex h-full items-center justify-center py-24">
      <div className="h-8 w-8 animate-spin rounded-full border-4 border-teal-600 border-t-transparent" />
    </div>
  );
}

function ProtectedLayout() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-teal-600 border-t-transparent" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return (
    <Layout>
      <Suspense fallback={<PageLoader />}>
        <Outlet />
      </Suspense>
    </Layout>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedLayout />}>
        <Route index element={<DashboardPage />} />
        <Route path="agents/:agentType" element={<AgentPage />} />
        <Route path="reviews" element={<ReviewsPage />} />
        <Route path="reviews/:reviewId" element={<ReviewsPage />} />
        <Route path="workflows" element={<WorkflowsPage />} />
        <Route path="workflows/:workflowId" element={<WorkflowsPage />} />
        <Route path="payer-rules" element={<PayerRulesPage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
