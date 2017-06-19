from pyomo.environ import *
from pyomo.opt import TerminationCondition
import numpy as np
import pandas as pd

class CALVIN():

  def __init__(self, linksfile, ic=None):
    df = pd.read_csv(linksfile)
    df['link'] = df.i.map(str) + '_' + df.j.map(str) + '_' + df.k.map(str)
    df.set_index('link', inplace=True)
    self.nodes = pd.unique(df[['i','j']].values.ravel()).tolist()
    self.links = list(zip(df.i,df.j,df.k))

    self.df = df
    # self.T = len(self.df)
    SR_stats = pd.read_csv('calvin/data/SR_stats.csv', index_col=0).to_dict()
    self.min_storage = SR_stats['min']
    self.max_storage = SR_stats['max']

    if ic:
      self.apply_ic(ic)

    # a few network fixes to make things work
    self.add_ag_region_sinks()
    self.fix_hydropower_lbs()

    # make sure things aren't broken
    self.networkcheck()

  def apply_ic(self, ic):
    for k in ic:
      ix = (self.df.i.str.contains('INITIAL') &
            self.df.j.str.contains(k))
      self.df.loc[ix, ['lower_bound','upper_bound']] = ic[k]

  def inflow_multiplier(self, x):
    ix = self.df.i.str.contains('INFLOW')
    self.df.loc[ix, ['lower_bound','upper_bound']] *= x

  def eop_constraint_multiplier(self, x):
    for k in self.max_storage:
      ix = (self.df.i.str.contains(k) &
            self.df.j.str.contains('FINAL'))
      lb = self.min_storage[k] + (self.max_storage[k]-self.min_storage[k])*x
      self.df.loc[ix,'lower_bound'] = lb
      self.df.loc[ix,'upper_bound'] = self.max_storage[k]

  def no_gw_overdraft(self):
    pass
    #impose constraints..every year?

  def networkcheck(self):
    nodes = self.nodes
    links = self.df.values

    num_in = {n: 0 for n in nodes}
    num_out = {n: 0 for n in nodes}
    lb_in = {n: 0 for n in nodes} 
    lb_out = {n: 0 for n in nodes}
    ub_in = {n: 0 for n in nodes} 
    ub_out = {n: 0 for n in nodes}

    # loop over links
    for l in links:
      lb = float(l[5])
      ub = float(l[6])
      num_in[l[1]] += 1
      lb_in[l[1]] += lb
      ub_in[l[1]] += ub
      num_out[l[0]] += 1
      lb_out[l[0]] += lb
      ub_out[l[0]] += ub

      if lb > ub:
        raise ValueError('lb > ub for link %s' % (l[0]+'-'+l[1]))
    
    for n in nodes:
      if num_in[n] == 0 and n not in ['SOURCE','SINK']:
        raise ValueError('no incoming link for ' + n)
      if num_out[n] == 0 and n not in ['SOURCE','SINK']:
        raise ValueError('no outgoing link for ' + n)

      if ub_in[n] < lb_out[n]:
        raise ValueError('ub_in < lb_out for %s (%d < %d)' % (n, ub_in[n], lb_out[n]))
      if lb_in[n] > ub_out[n]:
        raise ValueError('lb_in > ub_out for %s (%d > %d)' % (n, lb_in[n], ub_out[n]))

  def add_ag_region_sinks(self):
    # hack to get rid of surplus water at no cost
    df = self.df
    links = df[df.i.str.contains('HSU') & ~df.j.str.contains('DBUG')].copy()
    maxub = links.upper_bound.max()
    links.j = links.apply(lambda l: 'SINK.'+l.i.split('.')[1], axis=1)
    links.cost = 0.0
    links.amplitude = 1.0
    links.lower_bound = 0.0
    links.upper_bound = maxub
    links['link'] = links.i.map(str) + '_' + links.j.map(str) + '_' + links.k.map(str)
    links.set_index('link', inplace=True)
    self.df.append(links)


  def fix_hydropower_lbs(self):
    # storage piecewise links > 0 should have 0.0 lower bound
    # the k=0 pieces should always have lb = dead pool
    def get_lb(link):
      if link.i.split('.')[0] == link.j.split('.')[0]:
        if link.k > 0:
          return 0.0
        elif link.i.split('.')[0] in self.min_storage:
          return min(self.min_storage[link.i.split('.')[0]], link.lower_bound)
      return link.lower_bound

    ix = (self.df.i.str.contains('SR_') & self.df.j.str.contains('SR_'))
    self.df.loc[ix, 'lower_bound'] = self.df.loc[ix].apply(get_lb, axis=1)

  def remove_debug_links(self):
    df = self.df
    ix = df.index[df.index.str.contains('DBUG')]
    df.drop(ix, inplace=True, axis=0)
    self.nodes = pd.unique(df[['i','j']].values.ravel()).tolist()
    self.links = list(zip(df.i,df.j,df.k))
    return df


  def create_pyomo_model(self, debug_mode=False, debug_cost=2e7):

    # work on a local copy of the dataframe
    if not debug_mode and self.df.index.str.contains('DBUG').any():
      df = self.remove_debug_links()
    else:
      df = self.df

    print('Creating Pyomo Model (debug=%s)' % debug_mode)

    model = ConcreteModel()

    model.N = Set(initialize=self.nodes)
    model.k = Set(initialize=range(15))
    model.A = Set(within=model.N*model.N*model.k, 
                  initialize=self.links, ordered=True)
    model.source = Param(initialize='SOURCE')
    model.sink = Param(initialize='SINK')

    def init_params(p):
      if p == 'cost' and debug_mode:
        return lambda model,i,j,k: debug_cost if ('DBUG' in str(i)+'_'+str(j)) else 1.0
      else:
        return lambda model,i,j,k: df.loc[str(i)+'_'+str(j)+'_'+str(k)][p]

    model.u = Param(model.A, initialize=init_params('upper_bound'), mutable=True)
    model.l = Param(model.A, initialize=init_params('lower_bound'), mutable=True)
    model.a = Param(model.A, initialize=init_params('amplitude'))
    model.c = Param(model.A, initialize=init_params('cost'))

    # The flow over each arc
    model.X = Var(model.A, within=Reals)

    # Minimize total cost
    def obj_fxn(model):
      return sum(model.c[i,j,k]*model.X[i,j,k] for (i,j,k) in model.A)
    model.total = Objective(rule=obj_fxn, sense=minimize)

    # Enforce an upper bound limit on the flow across each arc
    def limit_rule_upper(model, i, j, k):
      return model.X[i,j,k] <= model.u[i,j,k]
    model.limit_upper = Constraint(model.A, rule=limit_rule_upper)

    # Enforce a lower bound limit on the flow across each arc
    def limit_rule_lower(model, i, j, k):
      return model.X[i,j,k] >= model.l[i,j,k]
    model.limit_lower = Constraint(model.A, rule=limit_rule_lower)

    # To speed up creating the mass balance constraints, first
    # create dictionaries of arcs_in and arcs_out of every node
    # These are NOT Pyomo data, and Pyomo does not use "model._" at all
    arcs_in = {}
    arcs_out = {}

    def arc_list_hack(model, i,j,k):
      if j not in arcs_in:
        arcs_in[j] = []
      arcs_in[j].append((i,j,k))

      if i not in arcs_out:
        arcs_out[i] = []
      arcs_out[i].append((i,j,k))
      return [0]

    model._ = Set(model.A, initialize=arc_list_hack)

    # Enforce flow through each node (mass balance)
    def flow_rule(model, node):
      if node in [value(model.source), value(model.sink)]:
          return Constraint.Skip
      outflow  = sum(model.X[i,j,k]/model.a[i,j,k] for i,j,k in arcs_out[node])
      inflow = sum(model.X[i,j,k] for i,j,k in arcs_in[node])
      return inflow == outflow
    model.flow = Constraint(model.N, rule=flow_rule)

    model.dual = Suffix(direction=Suffix.IMPORT)

    self.model = model


  def solve_pyomo_model(self, solver='glpk', nproc=1, debug_mode=False, maxiter=10):
    from pyomo.opt import SolverFactory
    opt = SolverFactory(solver)

    if nproc > 1 and solver is not 'glpk':
      opt.options['threads'] = nproc
    
    if debug_mode:
      run_again = True
      i = 0
      vol_total = 0

      while run_again and i < maxiter:
        print('-----Solving Pyomo Model (debug=%s)' % debug_mode)
        self.results = opt.solve(self.model)
        print('Finished. Fixing debug flows...')
        run_again,vol = self.fix_debug_flows()
        i += 1
        vol_total += vol

      if run_again:
        raise RuntimeError('Debug mode failed: Maximum iterations reached')
      else:
        print('All debug flows eliminated (iter=%d, vol=%0.2f)' % (i,vol_total))

    else:
      print('-----Solving Pyomo Model (debug=%s)' % debug_mode)
      self.results = opt.solve(self.model, tee=False)

      if self.results.solver.termination_condition == TerminationCondition.optimal:
        print('Optimal Solution Found (debug=%s).' % debug_mode)
        self.model.solutions.load_from(self.results)
      else:
        raise RuntimeError('Problem Infeasible. Run again starting from debug mode.')


  def fix_debug_flows(self, tol=1e-5):

    df, model = self.df, self.model
    dbix = (df.i.str.contains('DBUGSRC') | df.j.str.contains('DBUGSNK'))
    debuglinks = df[dbix].values

    run_again = False
    vol_total = 0

    for dbl in debuglinks:
      s = tuple(dbl[0:3])

      if model.X[s].value > tol:
        run_again = True
        # print(s)
        # print(model.X[s].value)

        # if we need to get rid of extra water,
        # raise some upper bounds (just do them all)
        if 'DBUGSNK' in dbl[1]:
          raiselinks = df[(df.i == dbl[0]) & ~ df.j.str.contains('DBUGSNK')].values

          for l in raiselinks:
            s2 = tuple(l[0:3])
            iv = model.u[s2].value
            v = model.X[s].value*1.2
            model.u[s2].value += v
            vol_total += v
            print('%s UB raised by %0.2f (%0.2f%%)' % (l[0]+'_'+l[1], v, v*100/iv))
            df.loc['_'.join(str(x) for x in l[0:3]), 'upper_bound'] = model.u[s2].value

        # if we need to bring in extra water
        # this is a much bigger problem
        if 'DBUGSRC' in dbl[0]:
          vol_to_reduce = model.X[s].value*1.2
          reducelinks = df[(df.i == dbl[1]) & (df.lower_bound > 0)].values

          if reducelinks.size == 0:
            raise RuntimeError(('Not possible to reduce LB on links'
                                ' with origin %s by volume %0.2f' % 
                                (dbl[1],vol_to_reduce)))

          for l in reducelinks:
            s2 = tuple(l[0:3])
            iv = model.l[s2].value
            if iv > 0 and vol_to_reduce > 0:
              v = min(vol_to_reduce, iv)
              model.l[s2].value -= v
              vol_to_reduce -= v
              vol_total += v
              print('%s LB reduced by %0.2f (%0.2f%%)' % (l[0]+'_'+l[1], v, v*100/iv))
              df.loc['_'.join(str(x) for x in l[0:3]), 'lower_bound'] = model.l[s2].value
              
              if vol_to_reduce == 0:
                break

    self.df, self.model = df, model
    return run_again,vol_total
