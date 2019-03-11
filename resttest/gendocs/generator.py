import typing
from inspect import getsource

import redbaron

import resttest
from resttest.gendocs.meta import *
from resttest.gendocs.renderer import Renderer


def atomtrailers(node):
    if isinstance(node, redbaron.AtomtrailersNode):
        return node.value
    elif isinstance(node, redbaron.NameNode):
        return [node]
    elif isinstance(node, redbaron.DotProxyList) or isinstance(node, list):
        return node


def atomcall(node):
    trailers = atomtrailers(node)
    if not trailers:
        return

    *rest, call = trailers
    if not isinstance(call, redbaron.CallNode):
        return

    return rest, call.value


_builtins = {
    'None': None,
    'dict': dict,
    'list': list,
    'str': str,
    'True': True,
    'False': False,
}


class Context:
    def __init__(self, module, locals):
        self.module = module
        self.locals = {}
        self.http_response = None
        self.inline_next_call = False

        for k, v in locals.items():
            self[k] = v

    def __getitem__(self, name):
        try:
            return getattr(self.module, name)
        except AttributeError:
            try:
                return self.locals[name]
            except KeyError:
                try:
                    return _builtins[name]
                except KeyError:
                    raise NameError(name)

    def __setitem__(self, name, value):
        self.locals[name] = value
        value.var_name = name

    def eval(self, expr):
        if isinstance(expr, redbaron.EndlNode):
            return

        if isinstance(expr, redbaron.StringNode) or isinstance(expr, redbaron.IntNode):
            return eval(expr.value)

        if isinstance(expr, redbaron.InterpolatedStringNode):
            return eval(expr.value, {}, self)

        if isinstance(expr, redbaron.nodes.ListNode):
            return [self.eval(node) for node in expr.value]

        if isinstance(expr, redbaron.nodes.DictNode):
            return {self.eval(kv.key): self.eval(kv.value) for kv in expr.value}

        trailers = atomtrailers(expr)
        if trailers:
            name_node, *rest = trailers
            obj = self[name_node.value]
            for trailer in rest:
                if isinstance(trailer, redbaron.NameNode):
                    obj = getattr(obj, trailer.value)
                elif isinstance(trailer, redbaron.CallNode):
                    callable = obj

                    if self.inline_next_call:
                        render_func(self.module, callable)

                    self.inline_next_call = False

                    if is_pure(callable):
                        args = []
                        kwargs = {}
                        for arg in trailer.value:
                            value = self.eval(arg.value)
                            if arg.target:
                                kwargs[str(arg.target)] = value
                            else:
                                args.append(value)
                        value = callable(*args, **kwargs)
                    else:
                        value = make_instance(return_type(callable))

                    if getattr(callable, '__func__', None) in [resttest.HTTPSession.get, resttest.HTTPSession.post, resttest.HTTPSession.patch, resttest.HTTPSession.put, resttest.HTTPSession.delete]:
                        method = callable.__func__.__name__.upper()
                        url = trailer.value[0].value
                        try:
                            data = trailer.value[1].value
                        except IndexError:
                            data = None

                        if data:
                            data = self.try_eval(data)

                        renderer.write_http_request(method, self.try_eval(url), data)

                        self.http_response = value

                    obj = value
                else:
                    print(trailer)
                    trailer.help()
            return obj

        if isinstance(expr, redbaron.AssignmentNode):
            var = expr.target
            val = self.eval(expr.value)

            assert isinstance(var, redbaron.NameNode) or isinstance(var, redbaron.TupleNode)

            items = [(var, val)] if isinstance(var, redbaron.NameNode) else zip(var.value, val)
            for var, val in items:
                self[var.value] = val
                if class_of(val).__doc__ and not is_instance_of(val, resttest.HTTPResponse) and not is_instance_of(val, str):
                    renderer.write_var(var.value, class_of(val).__doc__)

            return

        if isinstance(expr, redbaron.AssertNode):
            test = expr.value

            if isinstance(test, redbaron.ComparisonNode) or isinstance(test, redbaron.BinaryOperatorNode):
                left_expr = atomtrailers(test.first)
                if not left_expr or self.eval(left_expr[0]) != self.http_response:
                    print(expr)
                    expr.help()
                    return

                try:
                    resp, = left_expr
                    data = None
                except ValueError:
                    try:
                        resp, data = left_expr
                    except ValueError:
                        print(expr)
                        expr.help()
                        return

                if self.eval(resp) != self.http_response:
                    print(expr)
                    expr.help()
                    return

                if data and str(data) != 'data':
                    print(expr)
                    expr.help()
                    return

                operator = str(test.value)
                right_expr = atomtrailers(test.second)

                if operator == '|':
                    operator_expr, _args = atomcall(right_expr)
                    _arg, = _args

                    operator = self.eval(operator_expr)
                    right_expr = atomtrailers(_arg.value)

                response_class = None
                if data:
                    if self.http_response.of != resttest.HTTPResponse:
                        response_class = self.http_response.of
                else:
                    _response_class, _args = atomcall(right_expr)
                    response_class = self.eval(_response_class)
                    _arg, = _args
                    right_expr = atomtrailers(_arg.value)

                response_data = self.try_eval(right_expr)

                if not response_class:
                    if response_data is not None:
                        response_class = resttest.HTTP200_OK
                    else:
                        response_class = resttest.HTTP204_NoContent

                response = response_class(response_data)

                renderer.write_http_response(response)
                return

        if isinstance(expr, redbaron.CommentNode):
            if expr.value.startswith('# resttest.'):
                if expr.value.strip() == '# resttest.inline':
                    self.inline_next_call = True
            elif expr.value.startswith('##'):
                renderer.write_text(expr.value)
            else:
                renderer.write_text(expr.value[1:].lstrip())

            return

        if isinstance(expr, redbaron.TryNode):
            assert getattr(expr, 'else') == None
            assert getattr(expr, 'finally') == None

            pre_catch = expr.value
            for substmt in pre_catch:
                if str(substmt).replace(' ', '') == '1/0':
                    break
                self.eval(substmt)

            catch, = expr.excepts
            Exc = self.eval(catch.exception)
            exc = make_instance(Exc)
            self[catch.target.value] = exc
            self.http_response = exc

            post_catch = catch.value
            for substmt in post_catch:
                self.eval(substmt)

            return

        if isinstance(expr, redbaron.DefNode):
            for stmt in expr.value:
                self.eval(stmt)

            return

        if isinstance(expr, redbaron.ReturnNode):
            return

        if isinstance(expr, redbaron.PrintNode):
            return

        raise NotImplementedError(f'{type(expr)} is too hard.')

    def try_eval(self, expr):
        try:
            return self.eval(expr)
        except Exception as e:
            print("try_eval failed", type(e), e)
            pass

        return expr


def render_func(module, func):
    def_node = redbaron.RedBaron(getsource(func))[0]

    hints = typing.get_type_hints(func)
    args = {}

    for arg in def_node.arguments:
        name = arg.target.value
        args[name] = make_instance(hints.get(name))

    Context(module, args).eval(def_node)


renderer = None


def test_name_to_title(name):
    assert name.startswith('test_')
    title = name[5:].replace('_', ' ')
    title = title[0].upper() + title[1:]
    return title


def render_module(mod):
    global renderer
    mod_name = mod.__name__.split('.')[-1]
    title = test_name_to_title(mod_name)
    output_file = f'docs/{mod_name[5:]}.md'
    renderer = Renderer(title, output_file)

    for name, value in mod.__dict__.items():
        if not name.startswith('test_'):
            continue

        renderer.start_case(test_name_to_title(name))

        render_func(mod, value)